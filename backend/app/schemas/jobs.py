from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import ChunkStatus, JobLogLevel, JobRunStatus, JobTrigger


def validate_cron(value: str) -> str:
    try:
        CronSim(value, datetime(2000, 1, 1))
    except CronSimError as exc:
        raise ValueError(f"Ekspresi cron tidak valid: {exc}") from exc
    return value


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Zona waktu '{value}' tidak dikenal.") from exc
    return value


class JobDefinitionRead(BaseModel):
    code: str
    description: str
    queue: str
    supports_dry_run: bool
    default_cron: str | None
    params_schema: dict[str, Any]


class JobRunCreate(BaseModel):
    job_code: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False


class JobRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_code: str
    status: JobRunStatus
    dry_run: bool
    params: dict[str, Any]
    trigger: JobTrigger
    schedule_id: UUID | None
    requested_by_user_id: UUID | None
    chunks_total: int
    chunks_done: int
    chunks_failed: int
    output: dict[str, Any]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class JobRunChunkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chunk_no: int
    status: ChunkStatus
    attempts: int
    params: dict[str, Any]
    output: dict[str, Any]
    error: str | None
    finished_at: datetime | None


class JobRunLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    level: JobLogLevel
    chunk_no: int | None
    message: str
    data: dict[str, Any] | None


class JobScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_code: str
    cron: str
    timezone: str
    params: dict[str, Any]
    is_active: bool
    next_run_at: datetime
    last_run_at: datetime | None


class JobScheduleUpdate(BaseModel):
    cron: str | None = Field(default=None, min_length=9, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    params: dict[str, Any] | None = None

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, value: str | None) -> str | None:
        return None if value is None else validate_cron(value)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str | None) -> str | None:
        return None if value is None else validate_timezone(value)
