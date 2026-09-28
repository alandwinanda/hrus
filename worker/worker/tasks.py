import asyncio
from uuid import UUID

from celery import Task as CeleryTask

from app.jobs import runner
from app.jobs.dispatch import TASK_RUN_CHUNK, TASK_START_RUN, celery_dispatch
from app.jobs.example import DEFAULT_CHUNK_SIZE, run_example_job
from worker.celery_app import QUEUE_DEFAULT, celery_app


@celery_app.task(name="jobs.example", queue=QUEUE_DEFAULT)
def example_job(total_items: int, chunk_size: int = DEFAULT_CHUNK_SIZE) -> dict[str, int]:
    """Contoh job dummy. Parameter task hanya nilai JSON, bukan object ORM."""
    return run_example_job(total_items, chunk_size).as_dict()


async def _start_run(tenant_id: UUID, run_id: UUID) -> None:
    async with runner.worker_sessions() as sessions:
        await runner.start_run(sessions, celery_dispatch, tenant_id, run_id)


async def _run_chunk(tenant_id: UUID, run_id: UUID, chunk_id: UUID) -> runner.ChunkOutcome:
    async with runner.worker_sessions() as sessions:
        return await runner.run_chunk(sessions, tenant_id, run_id, chunk_id)


@celery_app.task(name=TASK_START_RUN)
def start_run(tenant_id: str, run_id: str) -> None:
    """Pecah job run jadi chunk lalu antrekan tiap chunk. Status ada di tabel job_run."""
    asyncio.run(_start_run(UUID(tenant_id), UUID(run_id)))


@celery_app.task(name=TASK_RUN_CHUNK, bind=True, max_retries=None)
def run_chunk(self: CeleryTask, tenant_id: str, run_id: str, chunk_id: str) -> None:
    """Kerjakan satu chunk. Batas percobaan diatur definisi job (job_run_chunk.attempts)."""
    outcome = asyncio.run(_run_chunk(UUID(tenant_id), UUID(run_id), UUID(chunk_id)))
    if outcome.retry_in is not None:
        raise self.retry(countdown=outcome.retry_in)
