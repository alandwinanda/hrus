"""Kirim task ke Celery berdasarkan nama. Backend tidak meng-import kode worker.

Dispatch selalu dilakukan setelah transaksi yang membuat datanya commit. Kalau broker sedang
mati, job_run tetap tercatat dan scheduler mengirim ulang (lihat app/jobs/scheduler.py).
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from celery import Celery

from app.core.config import get_settings

TASK_START_RUN = "jobs.start_run"
TASK_RUN_CHUNK = "jobs.run_chunk"


@dataclass(frozen=True, slots=True)
class Task:
    name: str
    args: tuple[str, ...]
    queue: str


type Dispatch = Callable[[Task], None]


@lru_cache
def _client() -> Celery:
    return Celery("hrus", broker=get_settings().celery_broker_url)


def celery_dispatch(task: Task) -> None:
    _client().send_task(task.name, args=list(task.args), queue=task.queue)


def get_dispatch() -> Dispatch:
    """Dependency FastAPI, di-override di test."""
    return celery_dispatch
