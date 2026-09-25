from httpx import AsyncClient
from redis.asyncio import Redis

from app.core.db import build_engine, get_engine
from app.core.redis import get_redis
from app.main import app


async def test_health_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "backend"}


async def test_ready_when_database_and_redis_up(client: AsyncClient) -> None:
    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}


async def test_ready_returns_503_when_dependencies_down(client: AsyncClient) -> None:
    # Port 1 tidak pernah listen, jadi koneksi langsung ditolak.
    dead_engine = build_engine("postgresql+psycopg://x:y@127.0.0.1:1/none")
    dead_redis = Redis.from_url("redis://127.0.0.1:1/0")
    app.dependency_overrides[get_engine] = lambda: dead_engine
    app.dependency_overrides[get_redis] = lambda: dead_redis

    response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "error", "redis": "error"},
    }
    await dead_engine.dispose()
    await dead_redis.aclose()
