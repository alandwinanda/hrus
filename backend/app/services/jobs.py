"""Job run dan jadwal: dibuat dari API (HR/AI) atau scheduler, dieksekusi worker (ADR 009).

Fungsi yang perlu mengirim task mengembalikan daftar Task. Pemanggil mengirimnya setelah
transaksi commit, supaya worker tidak pernah menerima task untuk data yang belum tersimpan.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from cronsim import CronSim
from pydantic import ValidationError
from sqlalchemy import exists, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, RuleViolationError
from app.core.pagination import paginate
from app.core.security import AccessClaims
from app.jobs.definitions import DEFINITIONS, get_definition
from app.jobs.dispatch import TASK_RUN_CHUNK, TASK_START_RUN, Task
from app.models import (
    ChunkStatus,
    JobLogLevel,
    JobRun,
    JobRunChunk,
    JobRunLog,
    JobRunStatus,
    JobSchedule,
    JobTrigger,
)
from app.models.jobs import ACTIVE_RUN_STATUSES
from app.schemas.common import Page
from app.schemas.jobs import (
    JobDefinitionRead,
    JobRunChunkRead,
    JobRunLogRead,
    JobRunRead,
    JobScheduleRead,
    JobScheduleUpdate,
)
from app.services.audit import record_audit
from app.services.tenant import tenant_zone

RETRYABLE_STATUSES = (JobRunStatus.FAILED, JobRunStatus.PARTIAL)


def next_fire(cron: str, timezone: str, after: datetime) -> datetime:
    """Waktu jalan berikutnya setelah `after` menurut cron di zona waktu jadwal (DST aman)."""
    return next(CronSim(cron, after.astimezone(ZoneInfo(timezone)))).astimezone(UTC)


async def add_log(
    session: AsyncSession,
    run: JobRun,
    level: JobLogLevel,
    message: str,
    *,
    chunk_no: int | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    session.add(
        JobRunLog(
            tenant_id=run.tenant_id,
            job_run_id=run.id,
            chunk_no=chunk_no,
            level=level,
            message=message,
            data=data,
        )
    )


def list_definitions() -> list[JobDefinitionRead]:
    return [
        JobDefinitionRead(
            code=d.code,
            description=d.description,
            queue=d.queue,
            supports_dry_run=d.supports_dry_run,
            default_cron=d.default_cron,
            params_schema=d.params_model.model_json_schema(),
        )
        for d in DEFINITIONS.values()
    ]


async def create_run(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    job_code: str,
    params: dict[str, Any],
    dry_run: bool,
    trigger: JobTrigger,
    actor_user_id: UUID | None = None,
    schedule_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> tuple[JobRun, list[Task]]:
    definition = get_definition(job_code)
    if idempotency_key:
        existing = await session.scalar(
            select(JobRun).where(
                JobRun.tenant_id == tenant_id, JobRun.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing, []
    if dry_run and not definition.supports_dry_run:
        raise RuleViolationError("Job ini tidak mendukung dry-run.", code="dry_run_not_supported")
    try:
        parsed = definition.params_model.model_validate(params)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first["loc"])
        raise RuleViolationError(
            f"Parameter '{field}' tidak valid: {first['msg']}", code="invalid_job_params"
        ) from exc
    prepared = await definition.prepare(session, tenant_id, parsed)
    await _ensure_not_running(session, tenant_id, job_code)

    run = JobRun(
        tenant_id=tenant_id,
        job_code=job_code,
        status=JobRunStatus.QUEUED,
        dry_run=dry_run,
        params=prepared.model_dump(mode="json"),
        trigger=trigger,
        schedule_id=schedule_id,
        requested_by_user_id=actor_user_id,
        idempotency_key=idempotency_key,
    )
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
    except IntegrityError as exc:  # race: run lain untuk job yang sama baru saja dibuat
        raise _already_running(job_code) from exc
    await add_log(
        session,
        run,
        JobLogLevel.INFO,
        "Dry-run dibuat." if dry_run else "Run dibuat.",
        data={"trigger": trigger, "params": run.params},
    )
    await session.refresh(run)
    return run, [Task(TASK_START_RUN, (str(tenant_id), str(run.id)), definition.queue)]


def _already_running(job_code: str) -> ConflictError:
    return ConflictError(
        f"Job '{job_code}' masih berjalan atau antre. Tunggu selesai, atau batalkan dulu.",
        code="job_already_running",
    )


async def _ensure_not_running(session: AsyncSession, tenant_id: UUID, job_code: str) -> None:
    running = await session.scalar(
        select(
            exists().where(
                JobRun.tenant_id == tenant_id,
                JobRun.job_code == job_code,
                JobRun.status.in_(ACTIVE_RUN_STATUSES),
            )
        )
    )
    if running:
        raise _already_running(job_code)


async def _get_run(
    session: AsyncSession, tenant_id: UUID, run_id: UUID, *, lock: bool = False
) -> JobRun:
    stmt = select(JobRun).where(JobRun.tenant_id == tenant_id, JobRun.id == run_id)
    if lock:
        stmt = stmt.with_for_update()
    run = await session.scalar(stmt)
    if run is None:
        raise NotFoundError("Job run tidak ditemukan.")
    return run


async def get_run(session: AsyncSession, tenant_id: UUID, run_id: UUID) -> JobRunRead:
    return JobRunRead.model_validate(await _get_run(session, tenant_id, run_id))


async def list_runs(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    job_code: str | None,
    status: JobRunStatus | None,
    year: int | None,
    cursor: str | None,
    limit: int,
) -> Page[JobRunRead]:
    """Terbaru dulu. Default tahun berjalan (zona waktu tenant)."""
    zone = await tenant_zone(session, tenant_id)
    year = year or datetime.now(zone).year
    stmt = select(JobRun).where(
        JobRun.tenant_id == tenant_id,
        JobRun.created_at >= datetime(year, 1, 1, tzinfo=zone),
        JobRun.created_at < datetime(year + 1, 1, 1, tzinfo=zone),
    )
    if job_code is not None:
        stmt = stmt.where(JobRun.job_code == job_code)
    if status is not None:
        stmt = stmt.where(JobRun.status == status)
    page = await paginate(
        session, stmt, [JobRun.created_at, JobRun.id], cursor, limit, descending=True
    )
    return Page(
        items=[JobRunRead.model_validate(r) for r in page.items], next_cursor=page.next_cursor
    )


async def list_chunks(
    session: AsyncSession, tenant_id: UUID, run_id: UUID, *, cursor: str | None, limit: int
) -> Page[JobRunChunkRead]:
    await _get_run(session, tenant_id, run_id)
    stmt = select(JobRunChunk).where(
        JobRunChunk.tenant_id == tenant_id, JobRunChunk.job_run_id == run_id
    )
    page = await paginate(session, stmt, [JobRunChunk.chunk_no], cursor, limit)
    return Page(
        items=[JobRunChunkRead.model_validate(c) for c in page.items],
        next_cursor=page.next_cursor,
    )


async def list_logs(
    session: AsyncSession, tenant_id: UUID, run_id: UUID, *, cursor: str | None, limit: int
) -> Page[JobRunLogRead]:
    await _get_run(session, tenant_id, run_id)
    stmt = select(JobRunLog).where(JobRunLog.tenant_id == tenant_id, JobRunLog.job_run_id == run_id)
    page = await paginate(session, stmt, [JobRunLog.created_at, JobRunLog.id], cursor, limit)
    return Page(
        items=[JobRunLogRead.model_validate(entry) for entry in page.items],
        next_cursor=page.next_cursor,
    )


async def cancel_run(session: AsyncSession, actor: AccessClaims, run_id: UUID) -> JobRunRead:
    """Chunk yang sedang dikerjakan tetap selesai (satu transaksi); sisanya tidak dijalankan."""
    run = await _get_run(session, actor.tenant_id, run_id, lock=True)
    if run.status not in ACTIVE_RUN_STATUSES:
        raise RuleViolationError(
            "Hanya run yang antre atau berjalan yang bisa dibatalkan.", code="job_not_active"
        )
    await session.execute(
        update(JobRunChunk)
        .where(
            JobRunChunk.tenant_id == run.tenant_id,
            JobRunChunk.job_run_id == run.id,
            JobRunChunk.status == ChunkStatus.PENDING,
        )
        .values(status=ChunkStatus.CANCELLED)
    )
    run.status = JobRunStatus.CANCELLED
    run.finished_at = datetime.now(UTC)
    await add_log(
        session, run, JobLogLevel.WARNING, "Dibatalkan user.", data={"user_id": str(actor.user_id)}
    )
    await session.flush()
    await session.refresh(run)
    return JobRunRead.model_validate(run)


async def retry_run(
    session: AsyncSession, actor: AccessClaims, run_id: UUID
) -> tuple[JobRunRead, list[Task]]:
    """Ulangi chunk yang gagal saja. Chunk yang sudah sukses tidak dikerjakan ulang."""
    run = await _get_run(session, actor.tenant_id, run_id, lock=True)
    if run.status not in RETRYABLE_STATUSES:
        raise RuleViolationError(
            "Hanya run yang gagal atau gagal sebagian yang bisa diulang.", code="job_not_retryable"
        )
    await _ensure_not_running(session, run.tenant_id, run.job_code)
    definition = get_definition(run.job_code)
    retried = list(
        await session.scalars(
            update(JobRunChunk)
            .where(
                JobRunChunk.tenant_id == run.tenant_id,
                JobRunChunk.job_run_id == run.id,
                JobRunChunk.status == ChunkStatus.FAILED,
            )
            .values(status=ChunkStatus.PENDING, attempts=0, error=None, finished_at=None)
            .returning(JobRunChunk.id)
        )
    )
    # Gagal di tahap plan (belum ada chunk): mulai ulang dari awal.
    run.status = JobRunStatus.RUNNING if run.chunks_total else JobRunStatus.QUEUED
    run.error = None
    run.finished_at = None
    run.chunks_failed = 0
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise _already_running(run.job_code) from exc
    await add_log(
        session,
        run,
        JobLogLevel.INFO,
        f"Diulang user: {len(retried)} chunk.",
        data={"user_id": str(actor.user_id)},
    )
    await session.refresh(run)
    tenant = str(run.tenant_id)
    if not run.chunks_total:
        tasks = [Task(TASK_START_RUN, (tenant, str(run.id)), definition.queue)]
    else:
        tasks = [
            Task(TASK_RUN_CHUNK, (tenant, str(run.id), str(chunk_id)), definition.queue)
            for chunk_id in retried
        ]
    return JobRunRead.model_validate(run), tasks


# --- Jadwal -----------------------------------------------------------------------------


async def ensure_default_schedules(
    session: AsyncSession, tenant_id: UUID, timezone: str, *, now: datetime | None = None
) -> None:
    """Jadwal bawaan untuk tenant baru. Idempotent; jadwal yang sudah diubah HR tidak disentuh."""
    now = now or datetime.now(UTC)
    for definition in DEFINITIONS.values():
        if definition.default_cron is None:
            continue
        await session.execute(
            insert(JobSchedule)
            .values(
                tenant_id=tenant_id,
                job_code=definition.code,
                cron=definition.default_cron,
                timezone=timezone,
                next_run_at=next_fire(definition.default_cron, timezone, now),
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "job_code"])
        )


async def list_schedules(
    session: AsyncSession, tenant_id: UUID, *, cursor: str | None, limit: int
) -> Page[JobScheduleRead]:
    stmt = select(JobSchedule).where(JobSchedule.tenant_id == tenant_id)
    page = await paginate(session, stmt, [JobSchedule.job_code, JobSchedule.id], cursor, limit)
    return Page(
        items=[JobScheduleRead.model_validate(s) for s in page.items], next_cursor=page.next_cursor
    )


async def update_schedule(
    session: AsyncSession, actor: AccessClaims, schedule_id: UUID, data: JobScheduleUpdate
) -> JobScheduleRead:
    schedule = await session.scalar(
        select(JobSchedule)
        .where(JobSchedule.tenant_id == actor.tenant_id, JobSchedule.id == schedule_id)
        .with_for_update()
    )
    if schedule is None:
        raise NotFoundError("Jadwal job tidak ditemukan.")
    before = JobScheduleRead.model_validate(schedule)
    if data.params is not None:
        definition = get_definition(schedule.job_code)
        try:
            definition.params_model.model_validate(data.params)
        except ValidationError as exc:
            raise RuleViolationError(
                "Parameter jadwal tidak valid.", code="invalid_job_params"
            ) from exc
        schedule.params = data.params
    if data.cron is not None:
        schedule.cron = data.cron
    if data.timezone is not None:
        schedule.timezone = data.timezone
    if data.is_active is not None:
        schedule.is_active = data.is_active
    if data.cron is not None or data.timezone is not None:
        schedule.next_run_at = next_fire(schedule.cron, schedule.timezone, datetime.now(UTC))
    await session.flush()
    await session.refresh(schedule)
    after = JobScheduleRead.model_validate(schedule)
    if after != before:
        await record_audit(
            session,
            tenant_id=actor.tenant_id,
            actor_user_id=actor.user_id,
            action="job_schedule.update",
            entity_type="job_schedule",
            entity_id=schedule.id,
            before=before.model_dump(mode="json"),
            after=after.model_dump(mode="json"),
        )
    return after
