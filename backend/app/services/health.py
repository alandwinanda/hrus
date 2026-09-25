import asyncio
from collections.abc import Awaitable

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.logging import get_logger
from app.schemas.health import CheckStatus, ReadinessChecks, ReadinessResponse

logger = get_logger(__name__)


async def _ping_database(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _ping_redis(client: Redis) -> None:
    await client.ping()


async def _run_check(name: str, check: Awaitable[None], seconds: float) -> CheckStatus:
    try:
        async with asyncio.timeout(seconds):
            await check
    except Exception as exc:
        logger.warning("readiness_check_failed", check=name, error=type(exc).__name__)
        return "error"
    return "ok"


async def check_readiness(engine: AsyncEngine, redis: Redis, seconds: float) -> ReadinessResponse:
    """Cek database dan Redis secara paralel, masing-masing dengan batas waktu."""
    database, redis_status = await asyncio.gather(
        _run_check("database", _ping_database(engine), seconds),
        _run_check("redis", _ping_redis(redis), seconds),
    )
    checks = ReadinessChecks(database=database, redis=redis_status)
    ready = database == "ok" and redis_status == "ok"
    return ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)
