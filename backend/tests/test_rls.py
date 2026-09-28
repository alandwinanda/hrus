"""RLS diuji memakai user DB aplikasi (non-superuser), sama seperti di production."""

from datetime import date

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import hash_password
from app.core.tenant import set_tenant_context
from app.models import AppUser, AuditLog, Employee, EmployeeJob, OrgUnit, Tenant
from tests.conftest import MakeTenant, MakeUser


async def test_app_db_user_cannot_bypass_rls(session: AsyncSession) -> None:
    row = (
        await session.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )
    ).one()

    assert tuple(row) == (False, False)


async def test_every_tenant_table_has_forced_rls(session: AsyncSession) -> None:
    """Jaring pengaman: tabel baru dengan tenant_id yang lupa diberi RLS langsung ketahuan."""
    rows = await session.execute(
        text(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid) AS has_policy
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id'
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT a.attisdropped
            ORDER BY c.relname
            """
        )
    )
    tables = {
        row.relname: (row.relrowsecurity, row.relforcerowsecurity, row.has_policy) for row in rows
    }

    assert "leave_request" in tables
    assert {name: flags for name, flags in tables.items() if flags != (True, True, True)} == {}


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


async def test_composite_fk_blocks_cross_tenant_reference_even_for_owner(
    admin_sessionmaker: async_sessionmaker[AsyncSession], make_tenant: MakeTenant
) -> None:
    """Koneksi owner melewati RLS, tapi composite FK (tenant_id, id) tetap menolak."""
    tenant_a, tenant_b = await make_tenant(), await make_tenant()
    async with admin_sessionmaker() as s, s.begin():
        unit_b = OrgUnit(tenant_id=tenant_b.id, code="B", name="Unit B")
        employee_a = Employee(
            tenant_id=tenant_a.id, employee_number="A1", full_name="A", hire_date=date(2024, 1, 1)
        )
        s.add_all([unit_b, employee_a])
        await s.flush()

        with pytest.raises(IntegrityError, match="fk_employee_job_org_unit"):
            async with s.begin_nested():
                s.add(
                    EmployeeJob(
                        tenant_id=tenant_a.id,
                        employee_id=employee_a.id,
                        effdt=date(2024, 1, 1),
                        action="hire",
                        job_title="Staff",
                        grade="G1",
                        org_unit_id=unit_b.id,
                        employment_type="permanent",
                        employment_status="active",
                    )
                )
                await s.flush()


async def test_employee_job_history_is_append_only(
    session: AsyncSession, make_tenant: MakeTenant
) -> None:
    tenant = await make_tenant()
    await set_tenant_context(session, tenant.id)

    with pytest.raises(DBAPIError, match="permission denied"):
        await session.execute(update(EmployeeJob).values(grade="G9"))
