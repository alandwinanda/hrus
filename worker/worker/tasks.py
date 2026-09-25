from app.jobs.example import DEFAULT_CHUNK_SIZE, run_example_job
from worker.celery_app import QUEUE_DEFAULT, celery_app


@celery_app.task(name="jobs.example", queue=QUEUE_DEFAULT)
def example_job(total_items: int, chunk_size: int = DEFAULT_CHUNK_SIZE) -> dict[str, int]:
    """Contoh job dummy. Parameter task hanya nilai JSON, bukan object ORM."""
    return run_example_job(total_items, chunk_size).as_dict()
