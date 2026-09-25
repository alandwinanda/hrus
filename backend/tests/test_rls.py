"""RLS diuji memakai user DB aplikasi (non-superuser), sama seperti di production."""

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.core.tenant import set_tenant_context
from app.models import AppUser, AuditLog, Tenant
from tests.conftest import MakeTenant, MakeUser


async def test_app_db_user_cannot_bypass_rls(session: AsyncSession) -> None:
    row = (
        await session.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )
    ).one()

    assert tuple(row) == (False, False)


async def test_tenant_only_sees_own_rows_without_where(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant_a, tenant_b = await make_tenant(), await make_tenant()
    user_a = await make_user(tenant_a)
    await make_user(tenant_b)

    await set_tenant_context(session, tenant_a.id)
    # Sengaja tanpa filter tenant_id: RLS yang harus menyaring.
    emails = set(await session.scalars(select(AppUser.email)))

    assert emails == {user_a.email}


async def test_no_tenant_context_returns_nothing(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    await make_user(await make_tenant())

    count = await session.scalar(select(text("count(*)")).select_from(AppUser))

    assert count == 0


async def test_context_resets_after_transaction(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant = await make_tenant()
    await make_user(tenant)
    await set_tenant_context(session, tenant.id)
    assert await session.scalar(select(text("count(*)")).select_from(AppUser)) == 1

    await session.rollback()
    await session.begin()

    assert await session.scalar(select(text("count(*)")).select_from(AppUser)) == 0


async def test_cannot_write_into_other_tenant(
    session: AsyncSession, make_tenant: MakeTenant
) -> None:
    tenant_a, tenant_b = await make_tenant(), await make_tenant()
    await set_tenant_context(session, tenant_a.id)

    with pytest.raises(DBAPIError, match="row-level security"):
        await session.execute(
            insert(AppUser).values(
                tenant_id=tenant_b.id, email="susup@test.local", password_hash=hash_password("x")
            )
        )


async def test_cannot_update_other_tenant_rows(
    session: AsyncSession, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant_a, tenant_b = await make_tenant(), await make_tenant()
    victim = await make_user(tenant_b)
    await set_tenant_context(session, tenant_a.id)

    result = await session.execute(
        update(AppUser).where(AppUser.id == victim.id).values(is_active=False)
    )

    assert result.rowcount == 0  # type: ignore[attr-defined]


async def test_audit_log_is_append_only(session: AsyncSession, make_tenant: MakeTenant) -> None:
    tenant = await make_tenant()
    await set_tenant_context(session, tenant.id)
    session.add(AuditLog(tenant_id=tenant.id, action="test", entity_type="test"))
    await session.flush()

    with pytest.raises(DBAPIError, match="permission denied"):
        await session.execute(update(AuditLog).values(action="diubah"))


async def test_tenant_table_is_read_only_for_app(session: AsyncSession) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        await session.execute(insert(Tenant).values(slug="liar", name="Liar"))
