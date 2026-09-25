from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import bind_tenant

TENANT_SETTING = "app.tenant_id"
# Role grup PostgreSQL untuk aplikasi: NOLOGIN, tanpa BYPASSRLS. User login aplikasi
# (APP_DB_USER) menjadi anggota role ini.
APP_DB_ROLE = "hrus_app"


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


# --- Helper SQL untuk migrasi Alembic ------------------------------------------------
# Nama tabel berasal dari kode migrasi (konstanta), bukan input user.


def rls_statements(table: str) -> list[str]:
    """RLS isolasi tenant. Tanpa tenant context hasilnya 0 baris (fail closed).

    NULLIF diperlukan karena setelah set_config lokal berakhir, current_setting bisa
    mengembalikan string kosong, dan ''::uuid akan error.
    """
    current_tenant = f"NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = {current_tenant}) "
        f"WITH CHECK (tenant_id = {current_tenant})",
    ]


def grant_statement(table: str, privileges: str) -> str:
    return f"GRANT {privileges} ON {table} TO {APP_DB_ROLE}"


def create_app_role_statement() -> str:
    return f"""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_DB_ROLE}') THEN
            CREATE ROLE {APP_DB_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS;
        END IF;
    END
    $$;
    """
