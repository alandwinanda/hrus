from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tenant


async def tenant_today(session: AsyncSession, tenant_id: UUID) -> date:
    """Tanggal hari ini di zona waktu tenant. Dipakai sebagai default tanggal efektif."""
    tz = await session.scalar(select(Tenant.timezone).where(Tenant.id == tenant_id))
    return datetime.now(ZoneInfo(tz or "Asia/Jakarta")).date()
