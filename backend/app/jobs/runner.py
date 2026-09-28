"""Eksekusi job run per chunk (ADR 009). Dipanggil task Celery di worker/, atau langsung di test.

- start_run: kunci run, pecah data jadi chunk (plan), simpan chunk, lalu kirim task per chunk.
- run_chunk: satu transaksi = logic bisnis + status chunk. Baris chunk dikunci FOR UPDATE SKIP
  LOCKED, jadi task ganda (redelivery, scheduler) tidak pernah mengerjakan chunk yang sama dua
  kali. Dry-run menjalankan logic yang sama di savepoint lalu di-rollback.
- Setelah tiap chunk, status run dihitung ulang dari status chunk-nya.
"""

from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.db import build_engine
from app.core.logging import get_logger
from app.core.tenant import set_tenant_context
from app.jobs.definitions import get_definition
from app.jobs.dispatch import TASK_RUN_CHUNK, Dispatch, Task
from app.jobs.registry import ChunkContext
from app.models import ChunkStatus, JobLogLevel, JobRun, JobRunChunk, JobRunStatus
from app.services.jobs import add_log

logger = get_logger(__name__)

type SessionFactory = async_sessionmaker[AsyncSession]

MAX_ERROR_LENGTH = 1000
RETRY_BASE_SECONDS = 10


@asynccontextmanager
async def worker_sessions() -> AsyncIterator[SessionFactory]:
    """Session factory untuk satu task worker (engine tanpa pool, dibuang setelah task)."""
    engine = build_engine(get_settings().database_url, pooled=False)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@asynccontextmanager
async def _tenant_tx(sessions: SessionFactory, tenant_id: UUID) -> AsyncIterator[AsyncSession]:
    async with sessions() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        yield session


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]


async def start_run(
    sessions: SessionFactory, dispatch: Dispatch, tenant_id: UUID, run_id: UUID
) -> None:
    tasks: list[Task] = []
    async with _tenant_tx(sessions, tenant_id) as session:
        run = await session.scalar(
            select(JobRun)
            .where(JobRun.tenant_id == tenant_id, JobRun.id == run_id)
            .with_for_update()
        )
        if run is None or run.status != JobRunStatus.QUEUED:
            return  # sudah dimulai (task ganda) atau dibatalkan
        definition = get_definition(run.job_code)
        now = datetime.now(UTC)
        try:
            async with session.begin_nested():
                params = definition.params_model.model_validate(run.params)
                planned = await definition.plan(session, tenant_id, params)
        except Exception as exc:
            logger.exception("job_plan_failed", run_id=str(run_id), job_code=run.job_code)
            run.status = JobRunStatus.FAILED
            run.error = _error_text(exc)
            run.finished_at = now
            await add_log(
                session,
                run,
                JobLogLevel.ERROR,
                "Gagal menyiapkan chunk.",
                data={"error": run.error},
            )
            return

        chunks = [
            JobRunChunk(
                tenant_id=tenant_id,
                job_run_id=run.id,
                chunk_no=number,
                params=chunk_params,
                status=ChunkStatus.PENDING,
                attempts=0,
            )
            for number, chunk_params in enumerate(planned, start=1)
        ]
        session.add_all(chunks)
        run.status = JobRunStatus.RUNNING
        run.started_at = now
        run.chunks_total = len(chunks)
        if not chunks:
            run.status = JobRunStatus.SUCCESS
            run.finished_at = now
            run.output = {"counts": {}}
        await session.flush()
        await add_log(session, run, JobLogLevel.INFO, f"Mulai: {len(chunks)} chunk.")
        tasks = [
            Task(TASK_RUN_CHUNK, (str(tenant_id), str(run_id), str(chunk.id)), definition.queue)
            for chunk in chunks
        ]
    for task in tasks:
        dispatch(task)


@dataclass(frozen=True, slots=True)
class ChunkOutcome:
    """retry_in: detik sampai chunk dicoba lagi (None = tidak perlu diulang)."""

    retry_in: int | None = None


async def run_chunk(
    sessions: SessionFactory, tenant_id: UUID, run_id: UUID, chunk_id: UUID
) -> ChunkOutcome:
    try:
        executed = await _execute_chunk(sessions, tenant_id, run_id, chunk_id)
    except Exception as exc:
        logger.exception("job_chunk_failed", run_id=str(run_id), chunk_id=str(chunk_id))
        retry_in = await _record_failure(sessions, tenant_id, run_id, chunk_id, exc)
        await finalize(sessions, tenant_id, run_id)
        return ChunkOutcome(retry_in=retry_in)
    if executed:
        await finalize(sessions, tenant_id, run_id)
    return ChunkOutcome()


async def _execute_chunk(
    sessions: SessionFactory, tenant_id: UUID, run_id: UUID, chunk_id: UUID
) -> bool:
    async with _tenant_tx(sessions, tenant_id) as session:
        chunk = await session.scalar(
            select(JobRunChunk)
            .where(
                JobRunChunk.tenant_id == tenant_id,
                JobRunChunk.job_run_id == run_id,
                JobRunChunk.id == chunk_id,
                JobRunChunk.status == ChunkStatus.PENDING,
            )
            .with_for_update(skip_locked=True)
        )
        if chunk is None:
            return False  # sudah selesai, dibatalkan, atau sedang dikerjakan worker lain
        run = await session.scalar(
            select(JobRun).where(JobRun.tenant_id == tenant_id, JobRun.id == run_id)
        )
        if run is None or run.status != JobRunStatus.RUNNING:
            return False
        definition = get_definition(run.job_code)
        context = ChunkContext(
            tenant_id=tenant_id,
            run_id=run_id,
            chunk_no=chunk.chunk_no,
            params=definition.params_model.model_validate(run.params),
            chunk=chunk.params,
            dry_run=run.dry_run,
            actor_user_id=run.requested_by_user_id,
        )
        savepoint = await session.begin_nested()
        result = await definition.run_chunk(session, context)
        if run.dry_run:
            await savepoint.rollback()  # logic sama persis, perubahannya dibuang
        else:
            await savepoint.commit()
        chunk.status = ChunkStatus.SUCCESS
        chunk.attempts += 1
        chunk.error = None
        chunk.output = result.as_output()
        chunk.finished_at = datetime.now(UTC)
    return True


async def _record_failure(
    sessions: SessionFactory, tenant_id: UUID, run_id: UUID, chunk_id: UUID, exc: BaseException
) -> int | None:
    async with _tenant_tx(sessions, tenant_id) as session:
        chunk = await session.scalar(
            select(JobRunChunk)
            .where(JobRunChunk.tenant_id == tenant_id, JobRunChunk.id == chunk_id)
            .with_for_update()
        )
        run = await session.scalar(
            select(JobRun).where(JobRun.tenant_id == tenant_id, JobRun.id == run_id)
        )
        if chunk is None or run is None or chunk.status != ChunkStatus.PENDING:
            return None
        chunk.attempts += 1
        chunk.error = _error_text(exc)
        max_attempts = get_definition(run.job_code).max_attempts
        final = chunk.attempts >= max_attempts
        if final:
            chunk.status = ChunkStatus.FAILED
            chunk.finished_at = datetime.now(UTC)
        await add_log(
            session,
            run,
            JobLogLevel.ERROR if final else JobLogLevel.WARNING,
            f"Chunk {chunk.chunk_no} gagal (percobaan {chunk.attempts}/{max_attempts}).",
            chunk_no=chunk.chunk_no,
            data={"error": chunk.error},
        )
        return None if final else RETRY_BASE_SECONDS * 2 ** (chunk.attempts - 1)


async def finalize(sessions: SessionFactory, tenant_id: UUID, run_id: UUID) -> None:
    """Hitung ulang progress dari status chunk. Kalau tidak ada yang pending, run selesai."""
    async with _tenant_tx(sessions, tenant_id) as session:
        run = await session.scalar(
            select(JobRun)
            .where(JobRun.tenant_id == tenant_id, JobRun.id == run_id)
            .with_for_update()
        )
        if run is None or run.status != JobRunStatus.RUNNING:
            return
        by_status = dict(
            (
                await session.execute(
                    select(JobRunChunk.status, func.count())
                    .where(JobRunChunk.tenant_id == tenant_id, JobRunChunk.job_run_id == run_id)
                    .group_by(JobRunChunk.status)
                )
            ).all()
        )
        done = by_status.get(ChunkStatus.SUCCESS, 0)
        failed = by_status.get(ChunkStatus.FAILED, 0)
        run.chunks_done, run.chunks_failed = done, failed
        if by_status.get(ChunkStatus.PENDING, 0):
            run.updated_at = datetime.now(UTC)  # tanda masih hidup untuk scheduler
            return

        totals: Counter[str] = Counter()
        outputs = await session.scalars(
            select(JobRunChunk.output).where(
                JobRunChunk.tenant_id == tenant_id,
                JobRunChunk.job_run_id == run_id,
                JobRunChunk.status == ChunkStatus.SUCCESS,
            )
        )
        for output in outputs:
            totals.update(output.get("counts", {}))
        run.output = {"counts": dict(totals)}
        run.finished_at = datetime.now(UTC)
        if not failed:
            run.status = JobRunStatus.SUCCESS
        elif done:
            run.status = JobRunStatus.PARTIAL
        else:
            run.status = JobRunStatus.FAILED
        level = JobLogLevel.INFO if run.status == JobRunStatus.SUCCESS else JobLogLevel.ERROR
        await add_log(
            session,
            run,
            level,
            f"Selesai: {run.status} ({done} sukses, {failed} gagal).",
            data=run.output,
        )
