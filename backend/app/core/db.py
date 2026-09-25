from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


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
    return build_engine(get_settings().database_url)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependency FastAPI: satu session per request."""
    async with get_sessionmaker()() as session:
        yield session
