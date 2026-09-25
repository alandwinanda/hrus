from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api import health
from app.core.config import get_settings
from app.core.db import get_engine
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware
from app.core.pagination import InvalidCursorError
from app.core.redis import get_redis

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info(
        "startup",
        deployment_mode=settings.deployment_mode,
        ai_enabled=settings.ai_enabled,
    )
    yield
    await get_engine().dispose()
    await get_redis().aclose()
    logger.info("shutdown")


async def invalid_cursor_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": {"code": "invalid_cursor", "message": str(exc)}},
    )


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(service=settings.service_name, level=settings.log_level)

    app = FastAPI(title="AI-Native HRIS Core API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(InvalidCursorError, invalid_cursor_handler)
    app.include_router(health.router)
    return app


app = create_app()
