from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tenant


async def tenant_zone(session: AsyncSession, tenant_id: UUID) -> ZoneInfo:
    tz = await session.scalar(select(Tenant.timezone).where(Tenant.id == tenant_id))
    return ZoneInfo(tz or "Asia/Jakarta")


async def tenant_today(session: AsyncSession, tenant_id: UUID) -> date:
    """Tanggal hari ini di zona waktu tenant. Dipakai sebagai default tanggal efektif."""
    return datetime.now(await tenant_zone(session, tenant_id)).date()
