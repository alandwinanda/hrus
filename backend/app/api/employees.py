from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUserDep, TenantSessionDep, require_roles
from app.api.pagination import PageParamsDep
from app.models import Role
from app.schemas.common import Page
from app.schemas.employee import (
    EmployeeCreate,
    EmployeeJobCreate,
    EmployeeJobRead,
    EmployeeRead,
    EmployeeStatusFilter,
    EmployeeUpdate,
)
from app.services import employees as service

router = APIRouter(prefix="/employees", tags=["employees"])

HrAdmin = Annotated[object, Depends(require_roles(Role.HR_ADMIN))]
AsOf = Annotated[
    date | None, Query(description="Tanggal acuan jabatan. Default: hari ini di zona waktu tenant.")
]


@router.get("")
async def list_employees(
    user: CurrentUserDep,
    session: TenantSessionDep,
    page: PageParamsDep,
    _: HrAdmin,
    employment_status: Annotated[EmployeeStatusFilter, Query(alias="status")] = "active",
    org_unit_id: UUID | None = None,
    supervisor_id: UUID | None = None,
    as_of: AsOf = None,
) -> Page[EmployeeRead]:
    """Daftar karyawan beserta jabatan yang berlaku (MCP tool: list_employees, khusus HR)."""
    return await service.list_employees(
        session,
        user.tenant_id,
        cursor=page.cursor,
        limit=page.limit,
        status=employment_status,
        org_unit_id=org_unit_id,
        supervisor_id=supervisor_id,
        as_of=as_of,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_employee(
    body: EmployeeCreate, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> EmployeeRead:
    return await service.create_employee(session, user, body)


@router.get("/{employee_id}")
async def get_employee(
    employee_id: UUID, user: CurrentUserDep, session: TenantSessionDep, as_of: AsOf = None
) -> EmployeeRead:
    """HR: semua karyawan. Karyawan: dirinya sendiri. Atasan: bawahan langsung."""
    return await service.get_employee(session, user, employee_id, as_of=as_of)


@router.patch("/{employee_id}")
async def update_employee(
    employee_id: UUID,
    body: EmployeeUpdate,
    user: CurrentUserDep,
    session: TenantSessionDep,
    _: HrAdmin,
) -> EmployeeRead:
    return await service.update_employee(session, user, employee_id, body)


@router.get("/{employee_id}/jobs")
async def list_jobs(
    employee_id: UUID, user: CurrentUserDep, session: TenantSessionDep, page: PageParamsDep
) -> Page[EmployeeJobRead]:
    """Riwayat jabatan, terbaru dulu. HR atau karyawan itu sendiri."""
    return await service.list_jobs(session, user, employee_id, cursor=page.cursor, limit=page.limit)


@router.post("/{employee_id}/jobs", status_code=status.HTTP_201_CREATED)
async def add_job(
    employee_id: UUID,
    body: EmployeeJobCreate,
    user: CurrentUserDep,
    session: TenantSessionDep,
    _: HrAdmin,
) -> EmployeeJobRead:
    """Mutasi, promosi, perubahan data, berhenti, atau rehire. Riwayat lama tidak diubah."""
    return await service.add_job(session, user, employee_id, body)
