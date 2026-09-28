"""Leave Management: tipe cuti, policy jatah, hari libur, saldo, pengajuan, dan approval.

Saldo disimpan (bukan dihitung ulang dari histori) dan di-update di transaksi yang sama dengan
pengajuan/approval. Tumpang tindih pengajuan dicegah exclusion constraint di database (ADR 008).
"""

from datetime import date, datetime
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
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin

# Rentang kalender maksimal satu pengajuan. Cukup untuk cuti melahirkan 3 bulan (UU 13/2003)
# sampai 6 bulan (UU 4/2024 tentang KIA). Mengubahnya butuh migrasi check constraint.
MAX_REQUEST_SPAN_DAYS = 184


class LeaveRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"  # level berikutnya tidak diproses karena ditolak/dibatalkan


class HolidayKind(StrEnum):
    NATIONAL = "national"
    COLLECTIVE_LEAVE = "collective_leave"  # cuti bersama
    COMPANY = "company"


def _in(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(m.value) for m in enum)})"


class LeaveType(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "leave_type"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_leave_type_tenant_id_id"),
        Index("uq_leave_type_tenant_id_code", "tenant_id", "code", unique=True),
        CheckConstraint("approval_levels BETWEEN 1 AND 2", name="approval_levels_valid"),
        CheckConstraint("min_notice_days >= 0", name="min_notice_positive"),
        CheckConstraint(
            "max_days_per_request IS NULL OR max_days_per_request > 0", name="max_days_positive"
        ),
    )

    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    # False untuk cuti yang tidak memotong saldo, misal sakit dengan surat dokter.
    requires_balance: Mapped[bool] = mapped_column(server_default=true())
    is_paid: Mapped[bool] = mapped_column(server_default=true())
    min_notice_days: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    # True untuk cuti yang boleh diajukan setelah terjadi, misal sakit.
    allow_backdated: Mapped[bool] = mapped_column(server_default=false())
    max_days_per_request: Mapped[int | None] = mapped_column(SmallInteger)
    # 1 = atasan langsung, 2 = atasan langsung lalu atasannya.
    approval_levels: Mapped[int] = mapped_column(SmallInteger, server_default="1")
    is_active: Mapped[bool] = mapped_column(server_default=true())


class LeavePolicy(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Jatah tahunan per tipe cuti, grade (NULL = semua grade), dan minimal masa kerja."""

    __tablename__ = "leave_policy"
    __table_args__ = (
        Index(
            "uq_leave_policy_rule",
            "tenant_id",
            "leave_type_id",
            text("coalesce(grade, '')"),
            "min_service_months",
            unique=True,
        ),
        CheckConstraint("annual_days >= 0", name="annual_days_positive"),
        CheckConstraint("min_service_months >= 0", name="min_service_positive"),
        CheckConstraint("max_carry_over_days >= 0", name="carry_over_positive"),
        ForeignKeyConstraint(
            ["tenant_id", "leave_type_id"],
            ["leave_type.tenant_id", "leave_type.id"],
            name="fk_leave_policy_leave_type",
        ),
    )

    leave_type_id: Mapped[UUID]
    grade: Mapped[str | None] = mapped_column(String(20))
    min_service_months: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    annual_days: Mapped[int] = mapped_column(SmallInteger)
    max_carry_over_days: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    carry_over_expiry_months: Mapped[int] = mapped_column(SmallInteger, server_default="3")


class HolidayCalendar(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "holiday_calendar"
    __table_args__ = (
        Index("uq_holiday_calendar_tenant_id_date", "tenant_id", "holiday_date", unique=True),
        CheckConstraint(_in("kind", HolidayKind), name="kind_valid"),
    )

    holiday_date: Mapped[date]
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))


class LeaveBalance(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Saldo per karyawan per tipe per tahun. available = entitled + carried_over + adjusted
    - used - pending - expired. pending = hari yang sedang diajukan (belum final), expired =
    sisa carry-over yang hangus (carry-over dianggap dipakai lebih dulu)."""

    __tablename__ = "leave_balance"
    __table_args__ = (
        Index(
            "uq_leave_balance_key",
            "tenant_id",
            "employee_id",
            "leave_type_id",
            "year",
            unique=True,
        ),
        CheckConstraint("used >= 0 AND pending >= 0", name="usage_positive"),
        CheckConstraint("expired >= 0 AND expired <= carried_over", name="expired_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["employee.tenant_id", "employee.id"],
            name="fk_leave_balance_employee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "leave_type_id"],
            ["leave_type.tenant_id", "leave_type.id"],
            name="fk_leave_balance_leave_type",
        ),
    )

    employee_id: Mapped[UUID]
    leave_type_id: Mapped[UUID]
    year: Mapped[int] = mapped_column(SmallInteger)
    entitled: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    carried_over: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    adjusted: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    used: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    pending: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    expired: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    # Diisi job accrual (sekali per tahun) dan job hangus carry-over, supaya job idempotent.
    carry_over_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    carry_over_expires_on: Mapped[date | None]
    carry_over_expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def available(self) -> int:
        return (
            self.entitled
            + self.carried_over
            + self.adjusted
            - self.used
            - self.pending
            - self.expired
        )


class LeaveRequest(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "leave_request"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_leave_request_tenant_id_id"),
        Index(
            "ix_leave_request_tenant_id_employee_id_start", "tenant_id", "employee_id", "start_date"
        ),
        Index("ix_leave_request_tenant_id_status", "tenant_id", "status"),
        # Daftar semua pengajuan (HR, terbaru dulu) dan kalender (irisan rentang tanggal).
        # Btree, bukan GiST: operator && tidak leakproof, jadi tidak bisa jadi index condition
        # di bawah RLS. Exclusion constraint ex_leave_request_no_overlap ada di migrasi.
        Index("ix_leave_request_tenant_id_period", "tenant_id", "start_date", "end_date", "id"),
        Index(
            "uq_leave_request_idempotency",
            "tenant_id",
            "employee_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        CheckConstraint("end_date >= start_date", name="date_range_valid"),
        # Dijaga di database karena query irisan tanggal memakai batas bawah start_date ini.
        CheckConstraint(
            f"end_date - start_date < {MAX_REQUEST_SPAN_DAYS}", name="span_within_limit"
        ),
        CheckConstraint("days > 0", name="days_positive"),
        CheckConstraint("current_level BETWEEN 1 AND approval_levels", name="level_valid"),
        CheckConstraint(_in("status", LeaveRequestStatus), name="status_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["employee.tenant_id", "employee.id"],
            name="fk_leave_request_employee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "leave_type_id"],
            ["leave_type.tenant_id", "leave_type.id"],
            name="fk_leave_request_leave_type",
        ),
    )

    employee_id: Mapped[UUID]
    leave_type_id: Mapped[UUID]
    start_date: Mapped[date]
    end_date: Mapped[date]
    # Jumlah hari kerja, dihitung backend (bukan input user, bukan LLM).
    days: Mapped[int] = mapped_column(SmallInteger)
    reason: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20))
    approval_levels: Mapped[int] = mapped_column(SmallInteger)
    current_level: Mapped[int] = mapped_column(SmallInteger, server_default="1")
    requested_by_user_id: Mapped[UUID]
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    # Snapshot warning validasi saat diajukan (termasuk hasil AI nanti).
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LeaveApproval(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """Satu baris per level. Dibuat saat pengajuan, approver dari rantai atasan saat itu.
    approver_employee_id NULL = tidak ada atasan, diputuskan HR."""

    __tablename__ = "leave_approval"
    __table_args__ = (
        Index("uq_leave_approval_level", "tenant_id", "leave_request_id", "level", unique=True),
        Index("ix_leave_approval_inbox", "tenant_id", "approver_employee_id", "status"),
        CheckConstraint(_in("status", ApprovalStatus), name="status_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "leave_request_id"],
            ["leave_request.tenant_id", "leave_request.id"],
            name="fk_leave_approval_request",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "approver_employee_id"],
            ["employee.tenant_id", "employee.id"],
            name="fk_leave_approval_approver",
        ),
    )

    leave_request_id: Mapped[UUID]
    level: Mapped[int] = mapped_column(SmallInteger)
    approver_employee_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(String(500))
    decided_by_user_id: Mapped[UUID | None]
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
