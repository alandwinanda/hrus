from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models import ApprovalStatus, HolidayKind, LeaveRequestStatus

# --- Konfigurasi (HR) -------------------------------------------------------------------


class LeaveTypeCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=120)
    requires_balance: bool = True
    is_paid: bool = True
    min_notice_days: int = Field(default=0, ge=0, le=365)
    allow_backdated: bool = False
    max_days_per_request: int | None = Field(default=None, gt=0, le=365)
    approval_levels: int = Field(default=1, ge=1, le=2)


class LeaveTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_paid: bool | None = None
    min_notice_days: int | None = Field(default=None, ge=0, le=365)
    allow_backdated: bool | None = None
    max_days_per_request: int | None = Field(default=None, gt=0, le=365)
    approval_levels: int | None = Field(default=None, ge=1, le=2)
    is_active: bool | None = None


class LeaveTypeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    requires_balance: bool
    is_paid: bool
    min_notice_days: int
    allow_backdated: bool
    max_days_per_request: int | None
    approval_levels: int
    is_active: bool


class LeavePolicyCreate(BaseModel):
    leave_type_id: UUID
    grade: str | None = Field(default=None, min_length=1, max_length=20)
    min_service_months: int = Field(default=0, ge=0, le=600)
    annual_days: int = Field(ge=0, le=366)
    max_carry_over_days: int = Field(default=0, ge=0, le=366)
    carry_over_expiry_months: int = Field(default=3, ge=0, le=24)


class LeavePolicyUpdate(BaseModel):
    annual_days: int | None = Field(default=None, ge=0, le=366)
    max_carry_over_days: int | None = Field(default=None, ge=0, le=366)
    carry_over_expiry_months: int | None = Field(default=None, ge=0, le=24)


class LeavePolicyRead(LeavePolicyCreate):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class HolidayCreate(BaseModel):
    holiday_date: date
    name: str = Field(min_length=1, max_length=120)
    kind: HolidayKind = HolidayKind.NATIONAL


class HolidayRead(HolidayCreate):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


# --- Saldo ------------------------------------------------------------------------------


class LeaveBalanceRead(BaseModel):
    leave_type_id: UUID
    leave_type_code: str
    leave_type_name: str
    year: int
    entitled: int
    carried_over: int
    adjusted: int
    used: int
    pending: int
    expired: int
    carry_over_expires_on: date | None
    available: int


class LeaveBalanceSummary(BaseModel):
    employee_id: UUID
    year: int
    items: list[LeaveBalanceRead]


class BalanceAdjustment(BaseModel):
    """Koreksi saldo manual oleh HR (misal saldo awal migrasi dari sistem lama)."""

    employee_id: UUID
    leave_type_id: UUID
    year: int = Field(ge=2000, le=2100)
    delta: int = Field(ge=-366, le=366)
    note: str = Field(min_length=3, max_length=500)


# --- Pengajuan --------------------------------------------------------------------------


class LeaveRequestInput(BaseModel):
    leave_type_id: UUID
    start_date: date
    end_date: date
    reason: str | None = Field(default=None, max_length=500)


class RuleMessage(BaseModel):
    code: str
    message: str


class TeamOverlapWarning(RuleMessage):
    colleagues_on_leave: int


class BalancePreview(BaseModel):
    available_before: int
    available_after: int


class LeaveValidationResult(BaseModel):
    """Hasil dry-run. Hard error memblokir submit, warning hanya informasi."""

    valid: bool
    days: int
    errors: list[RuleMessage]
    warnings: list[TeamOverlapWarning]
    balance: BalancePreview | None


class LeaveApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    level: int
    approver_employee_id: UUID | None
    status: ApprovalStatus
    note: str | None
    decided_at: datetime | None


class LeaveRequestRead(BaseModel):
    id: UUID
    employee_id: UUID
    employee_name: str
    leave_type_id: UUID
    leave_type_code: str
    start_date: date
    end_date: date
    days: int
    reason: str | None
    status: LeaveRequestStatus
    approval_levels: int
    current_level: int
    created_at: datetime


class LeaveRequestDetail(LeaveRequestRead):
    approvals: list[LeaveApprovalRead]
    warnings: list[TeamOverlapWarning]


class LeaveDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=500)


class LeaveCancel(BaseModel):
    note: str | None = Field(default=None, max_length=500)


LeaveRequestScope = Literal["mine", "approvals", "all"]


class TeamCalendarItem(BaseModel):
    request_id: UUID
    employee_id: UUID
    employee_name: str
    leave_type_code: str
    start_date: date
    end_date: date
    days: int
    status: LeaveRequestStatus
