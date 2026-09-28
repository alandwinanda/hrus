import time
from functools import lru_cache
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ai_gateway.config import Settings, get_settings
from ai_gateway.logging import get_logger
from ai_gateway.providers import (
    ChatProvider,
    ProviderError,
    ProviderNotAllowedError,
    ProviderNotConfiguredError,
    build_provider,
)
from ai_gateway.schemas import ChatRequest, ChatResponse, ProviderCredentials

router = APIRouter()
logger = get_logger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]


@lru_cache
def get_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]


def get_provider(
    settings: Settings, client: httpx.AsyncClient, credentials: ProviderCredentials | None
) -> ChatProvider:
    if not settings.ai_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ai_disabled", "message": "AI tidak aktif, gunakan mode ERP."},
        )
    try:
        return build_provider(settings, client, credentials)
    except ProviderNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "base_url_not_allowed", "message": str(exc)},
        ) from exc
    except ProviderNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ai_not_configured", "message": str(exc)},
        ) from exc


class HealthResponse(BaseModel):
    status: str
    service: str


class ReadinessResponse(BaseModel):
    status: str
    ai_enabled: bool
    provider: str


@router.get("/health", tags=["health"])
async def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(status="ok", service=settings.service_name)


@router.get("/ready", tags=["health"])
async def ready(settings: SettingsDep) -> ReadinessResponse:
    return ReadinessResponse(
        status="ready", ai_enabled=settings.ai_enabled, provider=settings.llm_provider
    )


@router.post("/v1/chat", tags=["chat"])
async def chat(body: ChatRequest, settings: SettingsDep, client: HttpClientDep) -> ChatResponse:
    """Teruskan chat ke provider LLM. Isi pesan dan API key tidak pernah ditulis ke log.

    `credentials` diisi Core API dengan API key milik tenant (ADR 011). Tanpa itu, gateway
    memakai konfigurasi env (deployment dedicated).
    """
    provider = get_provider(settings, client, body.credentials)
    started = time.perf_counter()
    try:
        result = await provider.chat(body)
    except ProviderError as exc:
        logger.warning("llm_call_failed", provider=provider.name, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "provider_error", "message": "Provider LLM gagal, gunakan mode ERP."},
        ) from exc

    logger.info(
        "llm_call",
        provider=result.provider,
        model=result.model,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
        input_tokens=result.usage.input_tokens,
        cached_input_tokens=result.usage.cached_input_tokens,
        output_tokens=result.usage.output_tokens,
    )
    return result
