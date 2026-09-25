from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings


def build_engine(url: str) -> AsyncEngine:
    # prepare_threshold=None: prepared statement dimatikan supaya aman di PgBouncer
    # transaction mode (koneksi server bisa berganti di tiap transaksi).
    return create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args={"prepare_threshold": None},
    )


@lru_cache
def get_engine() -> AsyncEngine:
    """Engine aplikasi: user DB non-superuser, tunduk pada RLS."""
    return build_engine(get_settings().database_url)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def transaction_session() -> AsyncIterator[AsyncSession]:
    """Satu session + satu transaksi. Commit kalau sukses, rollback kalau ada exception.

    Tenant context (RLS) hanya berlaku di dalam transaksi ini.
    """
    async with get_sessionmaker()() as session, session.begin():
        yield session


def admin_engine(settings: Settings) -> AsyncEngine:
    """Engine owner/superuser untuk CLI admin (bikin tenant, user DB). Melewati RLS."""
    url = settings.migration_database_url
    if not url:
        raise RuntimeError("MIGRATION_DATABASE_URL wajib diisi untuk perintah admin")
    return build_engine(url)
