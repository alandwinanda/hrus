"""Operasi admin: dipanggil CLI dan test memakai koneksi owner, bukan lewat API."""

import re
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.core.tenant import APP_DB_ROLE, set_tenant_context
from app.models import AppUser, Role, Tenant, UserRole
from app.services.audit import record_audit

MIN_PASSWORD_LENGTH = 8
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DB_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password minimal {MIN_PASSWORD_LENGTH} karakter")


async def create_tenant(
    session: AsyncSession, *, slug: str, name: str, timezone: str = "Asia/Jakarta"
) -> Tenant:
    slug = slug.strip().lower()
    if not _SLUG.fullmatch(slug):
        raise ValueError("Slug hanya huruf kecil, angka, dan tanda minus (maks 63 karakter)")
    tenant = Tenant(slug=slug, name=name.strip(), timezone=timezone)
    session.add(tenant)
    await session.flush()
    return tenant


async def get_tenant_by_slug(session: AsyncSession, slug: str) -> Tenant | None:
    return await session.scalar(select(Tenant).where(Tenant.slug == slug.strip().lower()))


async def create_user(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    email: str,
    password: str,
    roles: Iterable[Role],
    employee_id: UUID | None = None,
) -> AppUser:
    validate_password(password)
    role_set = {Role(r) for r in roles}
    if not role_set:
        raise ValueError("User wajib punya minimal satu role")

    await set_tenant_context(session, tenant_id)
    user = AppUser(
        tenant_id=tenant_id,
        email=email.strip().lower(),
        password_hash=hash_password(password),
        employee_id=employee_id,
    )
    session.add(user)
    await session.flush()
    session.add_all(
        UserRole(tenant_id=tenant_id, app_user_id=user.id, role=role) for role in sorted(role_set)
    )
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=None,
        action="app_user.create",
        entity_type="app_user",
        entity_id=user.id,
        after={"email": user.email, "roles": sorted(role_set)},
    )
    await session.flush()
    return user


async def ensure_app_db_user(session: AsyncSession, *, username: str, password: str) -> None:
    """Buat atau perbarui user login aplikasi sebagai anggota role grup hrus_app.

    Role grup dibuat oleh migrasi, jadi jalankan `alembic upgrade head` dulu.
    """
    if not _DB_IDENTIFIER.fullmatch(username):
        raise ValueError("Nama user DB hanya huruf kecil, angka, dan underscore")
    validate_password(password)

    exists = await session.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": username}
    )
    verb = "ALTER" if exists else "CREATE"
    # DDL tidak menerima bind parameter, jadi quoting dilakukan PostgreSQL lewat format().
    statement = await session.scalar(
        text(
            f"SELECT format('{verb} ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', "
            "CAST(:u AS text), CAST(:p AS text))"
        ),
        {"u": username, "p": password},
    )
    await session.execute(text(str(statement)))
    grant = await session.scalar(
        text(f"SELECT format('GRANT {APP_DB_ROLE} TO %I', CAST(:u AS text))"), {"u": username}
    )
    await session.execute(text(str(grant)))
