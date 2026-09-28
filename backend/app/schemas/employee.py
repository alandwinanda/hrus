from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models import EmploymentStatus, EmploymentType, JobAction

EmployeeStatus = Literal["active", "terminated", "pre_hire"]
EmployeeStatusFilter = Literal["active", "terminated", "pre_hire", "all"]


def _normalize_email(value: str | None) -> str | None:
    return value.strip().lower() if value else None


WorkEmail = Annotated[
    str | None,
    Field(default=None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    AfterValidator(_normalize_email),
]


class JobFields(BaseModel):
    job_title: str = Field(min_length=1, max_length=120)
    grade: str = Field(min_length=1, max_length=20)
    org_unit_id: UUID
    supervisor_employee_id: UUID | None = None
    employment_type: EmploymentType = EmploymentType.PERMANENT


class EmployeeCreate(BaseModel):
    """Membuat karyawan sekaligus riwayat jabatan pertama (aksi 'hire' per hire_date)."""

    employee_number: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_./-]+$")
    full_name: str = Field(min_length=1, max_length=200)
    work_email: WorkEmail = None
    hire_date: date
    job: JobFields


class EmployeeUpdate(BaseModel):
    """Data dasar saja. Perubahan jabatan/unit/atasan lewat POST /employees/{id}/jobs."""

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    work_email: WorkEmail = None


class CurrentJob(BaseModel):
    effdt: date
    effseq: int
    action: JobAction
    job_title: str
    grade: str
    org_unit_id: UUID
    supervisor_employee_id: UUID | None
    employment_type: EmploymentType
    employment_status: EmploymentStatus


class EmployeeRead(BaseModel):
    id: UUID
    employee_number: str
    full_name: str
    work_email: str | None
    hire_date: date
    status: EmployeeStatus
    # Jabatan yang berlaku per tanggal as_of. Kosong kalau karyawan belum mulai bekerja.
    current_job: CurrentJob | None


class EmployeeJobCreate(BaseModel):
    """Field jabatan yang tidak dikirim diambil dari riwayat yang berlaku per effdt."""

    effdt: date
    action: JobAction
    reason: str | None = Field(default=None, max_length=500)
    job_title: str | None = Field(default=None, min_length=1, max_length=120)
    grade: str | None = Field(default=None, min_length=1, max_length=20)
    org_unit_id: UUID | None = None
    supervisor_employee_id: UUID | None = None
    employment_type: EmploymentType | None = None


class EmployeeJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    effdt: date
    effseq: int
    action: JobAction
    reason: str | None
    job_title: str
    grade: str
    org_unit_id: UUID
    supervisor_employee_id: UUID | None
    employment_type: EmploymentType
    employment_status: EmploymentStatus
    created_at: datetime
