"""Test memakai PostgreSQL dan Redis asli (bukan SQLite/fake) supaya RLS dan query ikut teruji.

Jalankan `docker compose up -d postgres redis` dulu, atau set TEST_* env var di bawah.

- TEST_MIGRATION_DATABASE_URL: owner/superuser, untuk migrasi dan menyiapkan data (melewati RLS).
- TEST_DATABASE_URL: user aplikasi non-superuser (dibuat otomatis), sama seperti production.
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import make_url

MIGRATION_URL = os.environ.get(
    "TEST_MIGRATION_DATABASE_URL",
    "postgresql+psycopg://hrus:hrus_dev_password@localhost:5432/hrus_test",
)
APP_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hrus_app_test:hrus_app_test_password@localhost:5432/hrus_test",
)

# Harus sebelum import app: settings dibaca dari env saat pertama dipakai.
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": APP_URL,
        "MIGRATION_DATABASE_URL": MIGRATION_URL,
        "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"),
        "AI_ENABLED": "false",
        "JWT_SECRET": "test-secret-yang-panjangnya-lebih-dari-32-karakter",
        "COOKIE_SECURE": "false",
        "REFRESH_COOKIE_PATH": "/auth",
    }
)

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.db import build_engine, get_sessionmaker
from app.core.redis import get_redis
from app.main import app as fastapi_app
from app.models import AppUser, Role, Tenant
from app.services import admin

BACKEND_DIR = Path(__file__).resolve().parents[1]
BUSINESS_TABLES = ("audit_log", "refresh_token", "user_role", "app_user", "tenant")
TEST_PASSWORD = "password-test-123"


@pytest.fixture(scope="session", autouse=True)
async def _database() -> AsyncIterator[None]:
    """Migrasi ke head, siapkan user DB aplikasi, dan kosongkan data dari run sebelumnya."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = MIGRATION_URL
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")

    app_url = make_url(APP_URL)
    engine = build_engine(MIGRATION_URL)
    async with async_sessionmaker(engine)() as session, session.begin():
        await admin.ensure_app_db_user(
            session, username=str(app_url.username), password=str(app_url.password)
        )
        await session.execute(text(f"TRUNCATE {', '.join(BUSINESS_TABLES)} CASCADE"))
    await engine.dispose()
    await get_redis().flushdb()
    yield


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    fastapi_app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Session user aplikasi (tunduk RLS) dengan transaksi yang selalu di-rollback."""
    async with get_sessionmaker()() as s:
        await s.begin()
        yield s
        await s.rollback()


@pytest.fixture(scope="session")
async def admin_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(MIGRATION_URL)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


type MakeTenant = Callable[[], Awaitable[Tenant]]
type MakeUser = Callable[..., Awaitable[AppUser]]


@pytest.fixture
def make_tenant(admin_sessionmaker: async_sessionmaker[AsyncSession]) -> MakeTenant:
    """Buat tenant baru dengan slug unik (commit, jadi terlihat oleh session aplikasi)."""

    async def _make() -> Tenant:
        async with admin_sessionmaker() as s, s.begin():
            slug = f"t-{uuid4().hex[:12]}"
            return await admin.create_tenant(s, slug=slug, name=f"Tenant {slug}")

    return _make


@pytest.fixture
def make_user(admin_sessionmaker: async_sessionmaker[AsyncSession]) -> MakeUser:
    async def _make(
        tenant: Tenant,
        roles: Iterable[Role] = (Role.EMPLOYEE,),
        *,
        email: str | None = None,
        password: str = TEST_PASSWORD,
        is_active: bool = True,
    ) -> AppUser:
        async with admin_sessionmaker() as s, s.begin():
            user = await admin.create_user(
                s,
                tenant_id=tenant.id,
                email=email or f"u-{uuid4().hex[:10]}@test.local",
                password=password,
                roles=roles,
            )
            user.is_active = is_active
            return user

    return _make
