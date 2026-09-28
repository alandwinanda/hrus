"""Model ORM SQLAlchemy. Import semua model di sini supaya terbaca Alembic autogenerate."""

from app.models.ai import AiProvider, AiUsage, AiUsageMonthly, TenantAiSetting
from app.models.audit import AuditLog
from app.models.auth import AppUser, RefreshToken, Role, UserRole
from app.models.base import Base
from app.models.core_hr import (
    Employee,
    EmployeeJob,
    EmploymentStatus,
    EmploymentType,
    JobAction,
    OrgUnit,
)
from app.models.jobs import (
    ChunkStatus,
    JobLogLevel,
    JobRun,
    JobRunChunk,
    JobRunLog,
    JobRunStatus,
    JobSchedule,
    JobTrigger,
)
from app.models.leave import (
    ApprovalStatus,
    HolidayCalendar,
    HolidayKind,
    LeaveApproval,
    LeaveBalance,
    LeavePolicy,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)
from app.models.tenant import Tenant

__all__ = [
    "AiProvider",
    "AiUsage",
    "AiUsageMonthly",
    "AppUser",
    "ApprovalStatus",
    "AuditLog",
    "Base",
    "ChunkStatus",
    "Employee",
    "EmployeeJob",
    "EmploymentStatus",
    "EmploymentType",
    "HolidayCalendar",
    "HolidayKind",
    "JobAction",
    "JobLogLevel",
    "JobRun",
    "JobRunChunk",
    "JobRunLog",
    "JobRunStatus",
    "JobSchedule",
    "JobTrigger",
    "LeaveApproval",
    "LeaveBalance",
    "LeavePolicy",
    "LeaveRequest",
    "LeaveRequestStatus",
    "LeaveType",
    "OrgUnit",
    "RefreshToken",
    "Role",
    "Tenant",
    "TenantAiSetting",
    "UserRole",
]
