"""Framework batch job (ADR 009): jadwal, run, chunk, dan log. Setara Process Scheduler PeopleSoft.

`job_run` adalah sumber kebenaran status job, bukan broker Celery. Definisi job (kode, antrian,
ukuran chunk, parameter) ada di kode (`app/jobs/registry.py`), bukan di tabel.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class JobRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"  # sebagian chunk gagal, bisa di-retry
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobTrigger(StrEnum):
    USER = "user"
    SCHEDULE = "schedule"


class ChunkStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobLogLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


ACTIVE_RUN_STATUSES = (JobRunStatus.QUEUED, JobRunStatus.RUNNING)
_ACTIVE_SQL = ", ".join(repr(s.value) for s in ACTIVE_RUN_STATUSES)


def _in(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(m.value) for m in enum)})"


class JobSchedule(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Jadwal cron per tenant. HR bisa mengubah jam atau menonaktifkan tanpa deploy."""

    __tablename__ = "job_schedule"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_job_schedule_tenant_id_id"),
        Index("uq_job_schedule_tenant_id_job_code", "tenant_id", "job_code", unique=True),
        # Scan scheduler lintas tenant: pengecualian aturan index diawali tenant_id.
        Index("ix_job_schedule_due", "next_run_at", postgresql_where=text("is_active")),
    )

    job_code: Mapped[str] = mapped_column(String(64))
    cron: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    is_active: Mapped[bool] = mapped_column(server_default=true())
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobRun(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "job_run"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_job_run_tenant_id_id"),
        Index("ix_job_run_tenant_id_created", "tenant_id", "created_at", "id"),
        # Satu run aktif per tenant + jenis job (pengganti advisory lock yang tahan restart).
        Index(
            "uq_job_run_active",
            "tenant_id",
            "job_code",
            unique=True,
            postgresql_where=text(f"status IN ({_ACTIVE_SQL})"),
        ),
        Index(
            "ix_job_run_active_updated",
            "updated_at",
            postgresql_where=text(f"status IN ({_ACTIVE_SQL})"),
        ),
        Index(
            "uq_job_run_idempotency",
            "tenant_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        CheckConstraint(_in("status", JobRunStatus), name="status_valid"),
        CheckConstraint(_in("trigger", JobTrigger), name="trigger_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "schedule_id"],
            ["job_schedule.tenant_id", "job_schedule.id"],
            name="fk_job_run_schedule",
        ),
    )

    job_code: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    dry_run: Mapped[bool] = mapped_column(server_default=false())
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    trigger: Mapped[str] = mapped_column(String(16))
    schedule_id: Mapped[UUID | None]
    requested_by_user_id: Mapped[UUID | None]
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    chunks_total: Mapped[int] = mapped_column(server_default=text("0"))
    chunks_done: Mapped[int] = mapped_column(server_default=text("0"))
    chunks_failed: Mapped[int] = mapped_column(server_default=text("0"))
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobRunChunk(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Satu potongan data (misal 500 karyawan). Dikerjakan dan di-commit dalam satu transaksi
    bersama perubahan datanya, jadi restart cukup mengulang chunk yang belum sukses."""

    __tablename__ = "job_run_chunk"
    __table_args__ = (
        Index("uq_job_run_chunk_no", "tenant_id", "job_run_id", "chunk_no", unique=True),
        CheckConstraint(_in("status", ChunkStatus), name="status_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "job_run_id"],
            ["job_run.tenant_id", "job_run.id"],
            name="fk_job_run_chunk_run",
        ),
    )

    job_run_id: Mapped[UUID]
    chunk_no: Mapped[int]
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobRunLog(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """Log per langkah. Append-only: role aplikasi hanya punya SELECT dan INSERT."""

    __tablename__ = "job_run_log"
    __table_args__ = (
        Index("ix_job_run_log_run", "tenant_id", "job_run_id", "created_at", "id"),
        CheckConstraint(_in("level", JobLogLevel), name="level_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "job_run_id"],
            ["job_run.tenant_id", "job_run.id"],
            name="fk_job_run_log_run",
        ),
    )

    job_run_id: Mapped[UUID]
    chunk_no: Mapped[int | None]
    level: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
