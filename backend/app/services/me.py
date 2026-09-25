from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppUser, Tenant, UserRole
from app.schemas.auth import MeResponse, TenantSummary


async def get_profile(session: AsyncSession, user_id: UUID) -> MeResponse | None:
    row = (
        await session.execute(
            select(
                AppUser.id,
                AppUser.email,
                AppUser.employee_id,
                Tenant.id.label("tenant_id"),
                Tenant.slug,
                Tenant.name,
            )
            .join(Tenant, Tenant.id == AppUser.tenant_id)
            .where(AppUser.id == user_id, AppUser.is_active.is_(True))
        )
    ).one_or_none()
    if row is None:
        return None

    roles = await session.scalars(select(UserRole.role).where(UserRole.app_user_id == user_id))
    return MeResponse(
        id=row.id,
        email=row.email,
        roles=sorted(roles),
        employee_id=row.employee_id,
        tenant=TenantSummary(id=row.tenant_id, slug=row.slug, name=row.name),
    )
