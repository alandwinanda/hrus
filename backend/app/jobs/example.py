"""Contoh job dummy untuk memastikan jalur API → antrian → worker berjalan.

Polanya sama dengan job sungguhan: data dipecah per chunk, setiap chunk idempotent,
dan progress dicatat per chunk. Job sungguhan juga mencatat status di job_run.
"""

from dataclasses import asdict, dataclass

from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_CHUNK_SIZE = 500


@dataclass(frozen=True, slots=True)
class ExampleJobResult:
    total_items: int
    chunks: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def run_example_job(total_items: int, chunk_size: int = DEFAULT_CHUNK_SIZE) -> ExampleJobResult:
    if total_items < 0:
        raise ValueError("total_items tidak boleh negatif")
    if chunk_size < 1:
        raise ValueError("chunk_size minimal 1")

    chunks = 0
    for start in range(0, total_items, chunk_size):
        end = min(start + chunk_size, total_items)
        chunks += 1
        logger.info("example_job_chunk_done", chunk=chunks, start=start, end=end)

    return ExampleJobResult(total_items=total_items, chunks=chunks)
