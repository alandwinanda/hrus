"""Daftar semua job yang bisa dijalankan. Job baru didaftarkan di sini."""

from app.core.errors import NotFoundError
from app.jobs import leave
from app.jobs.registry import JobDefinition

DEFINITIONS: dict[str, JobDefinition] = {
    definition.code: definition for definition in (leave.ACCRUAL, leave.CARRY_OVER_EXPIRY)
}


def get_definition(code: str) -> JobDefinition:
    definition = DEFINITIONS.get(code)
    if definition is None:
        raise NotFoundError(f"Job '{code}' tidak dikenal.", code="job_not_found")
    return definition
