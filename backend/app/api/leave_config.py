from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUserDep, TenantSessionDep, require_roles
from app.api.pagination import PageParamsDep
from app.models import Role
from app.schemas.common import Page
from app.schemas.leave import (
    HolidayCreate,
    HolidayRead,
    LeavePolicyCreate,
    LeavePolicyRead,
    LeavePolicyUpdate,
    LeaveTypeCreate,
    LeaveTypeRead,
    LeaveTypeUpdate,
)
from app.services import leave_config as service
from app.services.tenant import tenant_today

router = APIRouter(prefix="/leave", tags=["leave-config"])

HrAdmin = Annotated[object, Depends(require_roles(Role.HR_ADMIN))]


@router.get("/types")
async def list_leave_types(
    user: CurrentUserDep,
    session: TenantSessionDep,
    page: PageParamsDep,
    include_inactive: bool = False,
) -> Page[LeaveTypeRead]:
    """Tipe cuti untuk form pengajuan. include_inactive hanya berlaku untuk HR."""
    return await service.list_leave_types(
        session,
        user.tenant_id,
        cursor=page.cursor,
        limit=page.limit,
        include_inactive=include_inactive and Role.HR_ADMIN in user.roles,
    )


@router.post("/types", status_code=status.HTTP_201_CREATED)
async def create_leave_type(
    body: LeaveTypeCreate, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> LeaveTypeRead:
    return await service.create_leave_type(session, user, body)


@router.patch("/types/{type_id}")
async def update_leave_type(
    type_id: UUID,
    body: LeaveTypeUpdate,
    user: CurrentUserDep,
    session: TenantSessionDep,
    _: HrAdmin,
) -> LeaveTypeRead:
    """Tipe cuti tidak dihapus, cukup dinonaktifkan (is_active=false) supaya histori utuh."""
    return await service.update_leave_type(session, user, type_id, body)


@router.get("/policies")
async def list_policies(
    user: CurrentUserDep,
    session: TenantSessionDep,
    page: PageParamsDep,
    _: HrAdmin,
    leave_type_id: UUID | None = None,
) -> Page[LeavePolicyRead]:
    return await service.list_policies(
        session, user.tenant_id, leave_type_id=leave_type_id, cursor=page.cursor, limit=page.limit
    )


@router.post("/policies", status_code=status.HTTP_201_CREATED)
async def create_policy(
    body: LeavePolicyCreate, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> LeavePolicyRead:
    return await service.create_policy(session, user, body)


@router.patch("/policies/{policy_id}")
async def update_policy(
    policy_id: UUID,
    body: LeavePolicyUpdate,
    user: CurrentUserDep,
    session: TenantSessionDep,
    _: HrAdmin,
) -> LeavePolicyRead:
    """Berlaku untuk saldo yang dibuat setelah ini. Saldo yang sudah ada tidak berubah."""
    return await service.update_policy(session, user, policy_id, body)


@router.delete("/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: UUID, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> None:
    await service.delete_policy(session, user, policy_id)


@router.get("/holidays")
async def list_holidays(
    user: CurrentUserDep,
    session: TenantSessionDep,
    page: PageParamsDep,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
) -> Page[HolidayRead]:
    """Hari libur nasional, cuti bersama, dan libur perusahaan. Default tahun berjalan."""
    year = year or (await tenant_today(session, user.tenant_id)).year
    return await service.list_holidays(
        session, user.tenant_id, year=year, cursor=page.cursor, limit=page.limit
    )


@router.post("/holidays", status_code=status.HTTP_201_CREATED)
async def create_holiday(
    body: HolidayCreate, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> HolidayRead:
    return await service.create_holiday(session, user, body)


@router.delete("/holidays/{holiday_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_holiday(
    holiday_id: UUID, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> None:
    """Pengajuan yang sudah dibuat tidak dihitung ulang."""
    await service.delete_holiday(session, user, holiday_id)
