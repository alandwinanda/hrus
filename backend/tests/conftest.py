"""Test memakai PostgreSQL dan Redis asli (bukan SQLite/fake) supaya RLS dan query ikut teruji.

Jalankan `docker compose up -d postgres redis` dulu, atau set TEST_DATABASE_URL/TEST_REDIS_URL.
"""

import os
from collections.abc import AsyncIterator

# Harus sebelum import app: settings dibaca dari env saat pertama dipakai.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hrus:hrus_dev_password@localhost:5432/hrus_test",
)
os.environ["MIGRATION_DATABASE_URL"] = os.environ["DATABASE_URL"]
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
os.environ["AI_ENABLED"] = "false"

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import app as fastapi_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    fastapi_app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Session dengan transaksi yang selalu di-rollback di akhir test."""
    async with get_sessionmaker()() as s:
        await s.begin()
        yield s
        await s.rollback()
