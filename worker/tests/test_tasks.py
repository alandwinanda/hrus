from worker.celery_app import celery_app
from worker.tasks import example_job


def test_queues_configured() -> None:
    names = {q.name for q in celery_app.conf.task_queues}

    assert names == {"high", "default", "low"}
    assert celery_app.conf.task_default_queue == "default"


def test_reliability_settings() -> None:
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.accept_content == ["json"]


def test_example_job_routed_to_default_queue() -> None:
    assert example_job.queue == "default"
    assert example_job.name == "jobs.example"


def test_example_job_runs_eagerly() -> None:
    result = example_job.apply(kwargs={"total_items": 1200, "chunk_size": 500})

    assert result.successful()
    assert result.get() == {"total_items": 1200, "chunks": 3}
