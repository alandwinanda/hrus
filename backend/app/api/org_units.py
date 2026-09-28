from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUserDep, TenantSessionDep, require_roles
from app.api.pagination import PageParamsDep
from app.models import Role
from app.schemas.common import Page
from app.schemas.org_unit import OrgUnitCreate, OrgUnitRead, OrgUnitUpdate
from app.services import org_units as service

router = APIRouter(prefix="/org-units", tags=["org-units"])

HrAdmin = Annotated[object, Depends(require_roles(Role.HR_ADMIN))]


@router.get("")
async def list_org_units(
    user: CurrentUserDep,
    session: TenantSessionDep,
    page: PageParamsDep,
    include_inactive: Annotated[bool, Query()] = False,
) -> Page[OrgUnitRead]:
    """Struktur organisasi. Bisa dilihat semua role."""
    return await service.list_org_units(
        session,
        user.tenant_id,
        cursor=page.cursor,
        limit=page.limit,
        include_inactive=include_inactive,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_org_unit(
    body: OrgUnitCreate, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> OrgUnitRead:
    return await service.create_org_unit(session, user, body)


@router.get("/{unit_id}")
async def get_org_unit(
    unit_id: UUID, user: CurrentUserDep, session: TenantSessionDep
) -> OrgUnitRead:
    return await service.get_org_unit(session, user.tenant_id, unit_id)


@router.patch("/{unit_id}")
async def update_org_unit(
    unit_id: UUID,
    body: OrgUnitUpdate,
    user: CurrentUserDep,
    session: TenantSessionDep,
    _: HrAdmin,
) -> OrgUnitRead:
    return await service.update_org_unit(session, user, unit_id, body)
