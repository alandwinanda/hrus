from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import bind_tenant

TENANT_SETTING = "app.tenant_id"


async def set_tenant_context(session: AsyncSession, tenant_id: UUID) -> None:
    """Set tenant untuk RLS di transaksi yang sedang berjalan.

    Memakai set_config(..., is_local => true), setara SET LOCAL: nilainya hilang saat
    transaksi selesai, jadi tidak bocor ke request lain yang memakai koneksi yang sama
    di PgBouncer.
    """
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": TENANT_SETTING, "value": str(tenant_id)},
    )
    bind_tenant(tenant_id)
