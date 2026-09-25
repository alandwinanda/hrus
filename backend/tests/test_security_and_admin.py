from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import cli
from app.core.config import Settings
from app.core.security import (
    InvalidTokenError,
    ensure_auth_configured,
    hash_password,
    new_refresh_token,
    refresh_token_tenant,
    verify_password,
)
from app.models import AppUser, UserRole
from app.services import admin
from tests.conftest import MIGRATION_URL


def test_verify_password() -> None:
    hashed = hash_password("rahasia-123")

    assert verify_password("rahasia-123", hashed) == (True, False)
    assert verify_password("salah", hashed) == (False, False)
    assert verify_password("apa-saja", None) == (False, False)
    assert verify_password("apa-saja", "bukan-hash-argon2") == (False, False)


def test_refresh_token_format() -> None:
    tenant_id = uuid4()
    raw, token_hash = new_refresh_token(tenant_id)

    assert refresh_token_tenant(raw) == tenant_id
    assert len(token_hash) == 64
    assert tenant_id.hex not in token_hash


@pytest.mark.parametrize("raw", ["", "tanpa-titik", "bukan-uuid.abc", f"{uuid4()}."])
def test_refresh_token_bad_format(raw: str) -> None:
    with pytest.raises(InvalidTokenError):
        refresh_token_tenant(raw)


@pytest.mark.parametrize("secret", [None, "pendek"])
def test_auth_requires_strong_secret(secret: str | None) -> None:
    settings = Settings(jwt_secret=SecretStr(secret) if secret else None)

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        ensure_auth_configured(settings)


@pytest.mark.parametrize(
    ("secret", "cookie_secure", "message"),
    [
        ("dev-only-" + "x" * 40, True, "nilai dev"),
        ("rahasia-production-" + "x" * 40, False, "COOKIE_SECURE"),
    ],
)
def test_production_rejects_dev_settings(secret: str, cookie_secure: bool, message: str) -> None:
    settings = Settings(
        app_env="production", jwt_secret=SecretStr(secret), cookie_secure=cookie_secure
    )

    with pytest.raises(RuntimeError, match=message):
        ensure_auth_configured(settings)


def test_production_accepts_strong_settings() -> None:
    settings = Settings(app_env="production", jwt_secret=SecretStr("r" * 48), cookie_secure=True)

    ensure_auth_configured(settings)


@pytest.mark.parametrize("slug", ["", "Huruf Besar", "-awal", "a" * 64, "pakai_underscore"])
async def test_create_tenant_rejects_bad_slug(
    admin_sessionmaker: async_sessionmaker[AsyncSession], slug: str
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        with pytest.raises(ValueError, match="Slug"):
            await admin.create_tenant(s, slug=slug, name="X")


async def test_create_user_rejects_short_password_and_no_roles(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        tenant = await admin.create_tenant(s, slug=f"t-{uuid4().hex[:8]}", name="X")
        with pytest.raises(ValueError, match="minimal"):
            await admin.create_user(s, tenant_id=tenant.id, email="a@b.c", password="123", roles=[])
        with pytest.raises(ValueError, match="role"):
            await admin.create_user(
                s, tenant_id=tenant.id, email="a@b.c", password="password-ok", roles=[]
            )


async def test_ensure_app_db_user_is_idempotent(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    name = f"hrus_app_tmp_{uuid4().hex[:8]}"
    async with admin_sessionmaker() as s, s.begin():
        await admin.ensure_app_db_user(s, username=name, password="password-1")
        await admin.ensure_app_db_user(s, username=name, password="password-2")
        row = (
            await s.execute(
                text(
                    "SELECT r.rolsuper, r.rolbypassrls, r.rolcanlogin, "
                    "pg_has_role(r.rolname, 'hrus_app', 'member') "
                    "FROM pg_roles r WHERE r.rolname = :n"
                ),
                {"n": name},
            )
        ).one()
        await s.execute(text(f'DROP ROLE "{name}"'))

    assert tuple(row) == (False, False, True, True)


@pytest.mark.parametrize("name", ["Pakai-Minus", "1angka", 'x"; DROP ROLE hrus_app; --'])
async def test_ensure_app_db_user_rejects_bad_name(
    admin_sessionmaker: async_sessionmaker[AsyncSession], name: str
) -> None:
    async with admin_sessionmaker() as s, s.begin():
        with pytest.raises(ValueError, match="Nama user DB"):
            await admin.ensure_app_db_user(s, username=name, password="password-ok")


async def test_seed_dev_is_idempotent(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(migration_database_url=MIGRATION_URL)

    await cli.seed_dev(settings, None)  # type: ignore[arg-type]
    await cli.seed_dev(settings, None)  # type: ignore[arg-type]

    async with admin_sessionmaker() as s, s.begin():
        tenant = await admin.get_tenant_by_slug(s, cli.DEV_TENANT_SLUG)
        assert tenant is not None
        emails = sorted(
            await s.scalars(select(AppUser.email).where(AppUser.tenant_id == tenant.id))
        )
        hr_roles = sorted(
            await s.scalars(
                select(UserRole.role)
                .join(AppUser, AppUser.id == UserRole.app_user_id)
                .where(AppUser.tenant_id == tenant.id, AppUser.email == "hr@demo.test")
            )
        )

    assert emails == sorted(email for email, _ in cli.DEV_USERS)
    assert hr_roles == ["employee", "hr_admin"]


async def test_seed_dev_refused_in_production() -> None:
    with pytest.raises(SystemExit, match="production"):
        await cli.seed_dev(Settings(app_env="production"), None)  # type: ignore[arg-type]
