import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app.jobs.example import run_example_job

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_example_job_splits_into_chunks() -> None:
    result = run_example_job(total_items=1200, chunk_size=500)

    assert result.as_dict() == {"total_items": 1200, "chunks": 3}


def test_example_job_empty() -> None:
    assert run_example_job(total_items=0).chunks == 0


@pytest.mark.parametrize(("total", "chunk"), [(-1, 500), (10, 0)])
def test_example_job_rejects_bad_input(total: int, chunk: int) -> None:
    with pytest.raises(ValueError):
        run_example_job(total_items=total, chunk_size=chunk)


def test_alembic_upgrade_head_runs() -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = os.environ["MIGRATION_DATABASE_URL"]
    config.attributes["configure_logger"] = False

    command.upgrade(config, "head")
