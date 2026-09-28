"""Core HR: struktur organisasi, data karyawan, dan riwayat jabatan (effective-dated).

Semua FK antar tabel tenant memakai composite key (tenant_id, id), jadi database sendiri
menolak referensi ke baris milik tenant lain (lihat ADR 007).
"""

from datetime import date
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class JobAction(StrEnum):
    HIRE = "hire"
    REHIRE = "rehire"
    TRANSFER = "transfer"
    PROMOTION = "promotion"
    DATA_CHANGE = "data_change"
    TERMINATION = "termination"


class EmploymentStatus(StrEnum):
    ACTIVE = "active"
    TERMINATED = "terminated"


class EmploymentType(StrEnum):
    PERMANENT = "permanent"  # PKWTT
    CONTRACT = "contract"  # PKWT
    INTERN = "intern"
    DAILY = "daily"


def _in(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


class OrgUnit(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Divisi/departemen, bertingkat lewat parent_id."""

    __tablename__ = "org_unit"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_org_unit_tenant_id_id"),
        Index("uq_org_unit_tenant_id_code", "tenant_id", "code", unique=True),
        Index("ix_org_unit_tenant_id_parent_id", "tenant_id", "parent_id"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
        ForeignKeyConstraint(
            ["tenant_id", "parent_id"],
            ["org_unit.tenant_id", "org_unit.id"],
            name="fk_org_unit_parent",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "manager_employee_id"],
            ["employee.tenant_id", "employee.id"],
            name="fk_org_unit_manager",
            use_alter=True,
        ),
    )

    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    parent_id: Mapped[UUID | None]
    manager_employee_id: Mapped[UUID | None]
    is_active: Mapped[bool] = mapped_column(server_default=true())


class Employee(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Data dasar karyawan. NIK KTP, gaji, dan rekening sengaja tidak ada di MVP.

    Jabatan, unit, atasan, dan status ada di employee_job (effective-dated).
    """

    __tablename__ = "employee"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_employee_tenant_id_id"),
        Index("uq_employee_tenant_id_employee_number", "tenant_id", "employee_number", unique=True),
        Index(
            "uq_employee_tenant_id_work_email",
            "tenant_id",
            "work_email",
            unique=True,
            postgresql_where=text("work_email IS NOT NULL"),
        ),
        CheckConstraint("work_email = lower(work_email)", name="work_email_lowercase"),
    )

    # Nomor induk karyawan dari perusahaan (bukan NIK KTP).
    employee_number: Mapped[str] = mapped_column(String(32))
    full_name: Mapped[str] = mapped_column(String(200))
    work_email: Mapped[str | None] = mapped_column(String(254))
    hire_date: Mapped[date]


class EmployeeJob(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Riwayat jabatan effective-dated (pola JOB PeopleSoft). Append-only: tidak pernah diubah.

    Baris yang berlaku pada tanggal X = effdt terbesar yang <= X, lalu effseq terbesar.
    Koreksi di tanggal yang sama memakai effseq berikutnya.
    """

    __tablename__ = "employee_job"
    __table_args__ = (
        Index(
            "uq_employee_job_effective",
            "tenant_id",
            "employee_id",
            "effdt",
            "effseq",
            unique=True,
        ),
        Index("ix_employee_job_tenant_id_org_unit_id", "tenant_id", "org_unit_id"),
        Index("ix_employee_job_tenant_id_supervisor", "tenant_id", "supervisor_employee_id"),
        CheckConstraint("effseq >= 0", name="effseq_positive"),
        CheckConstraint(
            "supervisor_employee_id IS NULL OR supervisor_employee_id <> employee_id",
            name="not_own_supervisor",
        ),
        CheckConstraint(_in("action", JobAction), name="action_valid"),
        CheckConstraint(_in("employment_status", EmploymentStatus), name="status_valid"),
        CheckConstraint(_in("employment_type", EmploymentType), name="type_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["employee.tenant_id", "employee.id"],
            name="fk_employee_job_employee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "org_unit_id"],
            ["org_unit.tenant_id", "org_unit.id"],
            name="fk_employee_job_org_unit",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "supervisor_employee_id"],
            ["employee.tenant_id", "employee.id"],
            name="fk_employee_job_supervisor",
        ),
    )

    employee_id: Mapped[UUID]
    effdt: Mapped[date]
    effseq: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    action: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(500))
    job_title: Mapped[str] = mapped_column(String(120))
    grade: Mapped[str] = mapped_column(String(20))
    org_unit_id: Mapped[UUID]
    supervisor_employee_id: Mapped[UUID | None]
    employment_type: Mapped[str] = mapped_column(String(20))
    employment_status: Mapped[str] = mapped_column(String(20))
