from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ai_gateway.api import get_http_client, router
from ai_gateway.config import get_settings
from ai_gateway.logging import configure_logging, get_logger
from ai_gateway.middleware import RequestContextMiddleware

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info(
        "startup",
        ai_enabled=settings.ai_enabled,
        provider=settings.llm_provider,
        model=settings.llm_model,
    )
    yield
    await get_http_client().aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(service=settings.service_name, level=settings.log_level)

    app = FastAPI(title="AI-Native HRIS AI Gateway", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(router)
    return app


app = create_app()
