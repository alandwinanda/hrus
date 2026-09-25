from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings, get_settings
from app.core.db import get_engine
from app.core.redis import get_redis
from app.schemas.health import HealthResponse, ReadinessResponse
from app.services import health as health_service

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """Proses hidup. Dipakai load balancer dan liveness probe."""
    return HealthResponse(status="ok", service=settings.service_name)


@router.get(
    "/ready",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def ready(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ReadinessResponse:
    """Siap menerima traffic: database dan Redis terhubung."""
    result = await health_service.check_readiness(engine, redis, settings.ready_timeout_seconds)
    if result.status != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
