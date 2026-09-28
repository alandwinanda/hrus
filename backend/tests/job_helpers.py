"""Helper test job: dispatcher yang mencatat task lalu menjalankannya langsung (tanpa Celery)."""

from typing import Any
from uuid import UUID

from httpx import AsyncClient

from app.core.db import get_sessionmaker
from app.jobs import runner
from app.jobs.dispatch import TASK_RUN_CHUNK, TASK_START_RUN, Task


class InlineDispatch:
    """Pengganti Celery. `drain()` mengerjakan task sampai habis; retry langsung diulang."""

    def __init__(self) -> None:
        self.queue: list[Task] = []
        self.sent: list[Task] = []

    def __call__(self, task: Task) -> None:
        self.queue.append(task)
        self.sent.append(task)

    async def drain(self, *, max_tasks: int = 500) -> int:
        sessions = get_sessionmaker()
        executed = 0
        while self.queue:
            executed += 1
            assert executed <= max_tasks, "task tidak habis-habis"
            task = self.queue.pop(0)
            ids = [UUID(arg) for arg in task.args]
            if task.name == TASK_START_RUN:
                await runner.start_run(sessions, self, ids[0], ids[1])
            elif task.name == TASK_RUN_CHUNK:
                outcome = await runner.run_chunk(sessions, ids[0], ids[1], ids[2])
                if outcome.retry_in is not None:
                    self.queue.append(task)
            else:  # pragma: no cover
                raise AssertionError(f"task tidak dikenal: {task.name}")
        return executed


async def run_job(
    client: AsyncClient,
    headers: dict[str, str],
    dispatch: InlineDispatch,
    job_code: str,
    *,
    dry_run: bool = False,
    **params: Any,
) -> dict[str, Any]:
    """Buat run lewat API, jalankan sampai selesai, lalu kembalikan status akhirnya."""
    response = await client.post(
        "/jobs/runs",
        json={"job_code": job_code, "params": params, "dry_run": dry_run},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    await dispatch.drain()
    final = await client.get(f"/jobs/runs/{response.json()['id']}", headers=headers)
    assert final.status_code == 200, final.text
    return final.json()
