"""Scheduler job (ADR 009). Jalankan: `python -m app.jobs.scheduler`.

Aman dijalankan lebih dari satu replika: tiap tick mengambil advisory lock transaksi (bukan
session, karena PgBouncer transaction mode), jadi hanya satu replika yang bekerja per tick.

Tiap tick:
- jadwal yang jatuh tempo -> buat job_run (dilewati kalau run sebelumnya masih jalan);
- run yang antre terlalu lama (task hilang, misal Redis restart) -> kirim ulang start_run;
- run berjalan yang lama tidak ada kemajuan -> kirim ulang chunk yang masih pending.
Semua task idempotent, jadi pengiriman ganda aman.
"""

import asyncio
import contextlib
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_engine, get_sessionmaker
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.core.tenant import set_tenant_context
from app.jobs.definitions import DEFINITIONS, get_definition
from app.jobs.dispatch import TASK_RUN_CHUNK, TASK_START_RUN, Dispatch, Task, celery_dispatch
from app.jobs.runner import SessionFactory
from app.models import ChunkStatus, JobRun, JobRunChunk, JobRunStatus, JobSchedule, JobTrigger
from app.services import jobs

logger = get_logger(__name__)

TICK_SECONDS = 30
SCHEDULER_LOCK_KEY = 7_210_009  # pg_try_advisory_xact_lock, satu tick per waktu
QUEUED_TIMEOUT = timedelta(minutes=5)
STALLED_TIMEOUT = timedelta(minutes=30)
BATCH_LIMIT = 200


@dataclass(slots=True)
class TickResult:
    leader: bool = True
    created: list[UUID] = field(default_factory=list)
    skipped: list[UUID] = field(default_factory=list)  # jadwal dilewati karena run masih jalan
    requeued: list[UUID] = field(default_factory=list)
    resumed: list[UUID] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)


async def tick(
    sessions: SessionFactory, dispatch: Dispatch, *, now: datetime | None = None
) -> TickResult:
    now = now or datetime.now(UTC)
    result = TickResult()
    async with sessions() as session, session.begin():
        leader = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": SCHEDULER_LOCK_KEY}
        )
        if not leader:
            return TickResult(leader=False)
        work = (
            await session.execute(
                text("SELECT kind, tenant_id, id FROM job_scheduler_work(:now, :q, :s, :lim)"),
                {
                    "now": now,
                    "q": now - QUEUED_TIMEOUT,
                    "s": now - STALLED_TIMEOUT,
                    "lim": BATCH_LIMIT,
                },
            )
        ).all()
        for kind, tenant_id, item_id in work:
            await set_tenant_context(session, tenant_id)
            if kind == "schedule":
                await _fire_schedule(session, tenant_id, item_id, now, result)
            elif kind == "queued":
                await _requeue(session, tenant_id, item_id, now, result)
            else:
                await _resume(session, tenant_id, item_id, now, result)
    for task in result.tasks:
        try:
            dispatch(task)
        except Exception:  # broker mati: dicoba lagi di tick berikutnya
            logger.exception("scheduler_dispatch_failed", task=task.name, args=task.args)
    return result


async def _fire_schedule(
    session: AsyncSession, tenant_id: UUID, schedule_id: UUID, now: datetime, result: TickResult
) -> None:
    schedule = await session.scalar(
        select(JobSchedule)
        .where(
            JobSchedule.tenant_id == tenant_id,
            JobSchedule.id == schedule_id,
            JobSchedule.is_active.is_(True),
            JobSchedule.next_run_at <= now,
        )
        .with_for_update(skip_locked=True)
    )
    if schedule is None:
        return
    # Jadwal yang terlewat (scheduler mati) tidak dikejar satu per satu: cukup sekali jalan.
    schedule.next_run_at = jobs.next_fire(schedule.cron, schedule.timezone, now)
    schedule.last_run_at = now
    if schedule.job_code not in DEFINITIONS:
        logger.warning("schedule_unknown_job", schedule_id=str(schedule_id), job=schedule.job_code)
        return
    try:
        async with session.begin_nested():
            run, tasks = await jobs.create_run(
                session,
                tenant_id,
                job_code=schedule.job_code,
                params=schedule.params,
                dry_run=False,
                trigger=JobTrigger.SCHEDULE,
                schedule_id=schedule.id,
            )
    except AppError as exc:  # misal run sebelumnya masih berjalan
        logger.warning("schedule_skipped", schedule_id=str(schedule_id), reason=exc.code)
        result.skipped.append(schedule.id)
        return
    result.created.append(run.id)
    result.tasks.extend(tasks)


async def _requeue(
    session: AsyncSession, tenant_id: UUID, run_id: UUID, now: datetime, result: TickResult
) -> None:
    run = await _lock_run(session, tenant_id, run_id, JobRunStatus.QUEUED)
    if run is None:
        return
    run.updated_at = now
    definition = get_definition(run.job_code)
    result.requeued.append(run.id)
    result.tasks.append(Task(TASK_START_RUN, (str(tenant_id), str(run.id)), definition.queue))


async def _resume(
    session: AsyncSession, tenant_id: UUID, run_id: UUID, now: datetime, result: TickResult
) -> None:
    run = await _lock_run(session, tenant_id, run_id, JobRunStatus.RUNNING)
    if run is None:
        return
    run.updated_at = now
    definition = get_definition(run.job_code)
    pending = await session.scalars(
        select(JobRunChunk.id).where(
            JobRunChunk.tenant_id == tenant_id,
            JobRunChunk.job_run_id == run.id,
            JobRunChunk.status == ChunkStatus.PENDING,
        )
    )
    result.resumed.append(run.id)
    result.tasks.extend(
        Task(TASK_RUN_CHUNK, (str(tenant_id), str(run.id), str(chunk_id)), definition.queue)
        for chunk_id in pending
    )


async def _lock_run(
    session: AsyncSession, tenant_id: UUID, run_id: UUID, status: JobRunStatus
) -> JobRun | None:
    return await session.scalar(
        select(JobRun)
        .where(JobRun.tenant_id == tenant_id, JobRun.id == run_id, JobRun.status == status)
        .with_for_update(skip_locked=True)
    )


async def main() -> None:
    settings = get_settings()
    configure_logging(service="scheduler", level=settings.log_level)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    logger.info("scheduler_started", tick_seconds=TICK_SECONDS)
    sessions = get_sessionmaker()
    while not stop.is_set():
        try:
            result = await tick(sessions, celery_dispatch)
            if result.created or result.requeued or result.resumed:
                logger.info(
                    "scheduler_tick",
                    created=len(result.created),
                    skipped=len(result.skipped),
                    requeued=len(result.requeued),
                    resumed=len(result.resumed),
                )
        except Exception:
            logger.exception("scheduler_tick_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
    await get_engine().dispose()
    logger.info("scheduler_stopped")


if __name__ == "__main__":
    asyncio.run(main())
