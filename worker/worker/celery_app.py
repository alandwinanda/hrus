from typing import Any

from celery import Celery, signals
from kombu import Queue

from app.core.config import get_settings
from app.core.logging import configure_logging

QUEUE_HIGH = "high"  # notifikasi, email
QUEUE_DEFAULT = "default"  # permintaan user
QUEUE_LOW = "low"  # maintenance, archiving

settings = get_settings()

celery_app = Celery("hrus", broker=settings.celery_broker_url, include=["worker.tasks"])
celery_app.conf.update(
    task_queues=(Queue(QUEUE_HIGH), Queue(QUEUE_DEFAULT), Queue(QUEUE_LOW)),
    task_default_queue=QUEUE_DEFAULT,
    # Status job disimpan di tabel job_run (PostgreSQL), bukan result backend Celery.
    task_ignore_result=True,
    # Task baru di-ack setelah selesai, jadi task yang terputus di tengah jalan diulang.
    # Konsekuensinya setiap job wajib idempotent.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    worker_hijack_root_logger=False,
)


@signals.setup_logging.connect
def _setup_logging(**_: Any) -> None:
    configure_logging(service="worker", level=settings.log_level)


@signals.task_prerun.connect
def _bind_task_context(task_id: str | None = None, task: Any = None, **_: Any) -> None:
    import structlog

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=None,
        tenant_id=None,
        task_id=task_id,
        task_name=getattr(task, "name", None),
    )
