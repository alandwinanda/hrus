import httpx

from ai_gateway.config import Settings
from ai_gateway.providers.base import (
    ChatProvider,
    ProviderError,
    ProviderNotAllowedError,
    ProviderNotConfiguredError,
)
from ai_gateway.providers.mock import MockProvider
from ai_gateway.providers.openai_compatible import OpenAICompatibleProvider
from ai_gateway.schemas import ProviderCredentials

__all__ = [
    "ChatProvider",
    "ProviderError",
    "ProviderNotAllowedError",
    "ProviderNotConfiguredError",
    "build_provider",
]


def build_provider(
    settings: Settings, client: httpx.AsyncClient, credentials: ProviderCredentials | None = None
) -> ChatProvider:
    """Kredensial tenant (BYOK) kalau dikirim, selain itu konfigurasi env (dedicated)."""
    if credentials is not None:
        base_url = credentials.base_url.rstrip("/")
        if base_url not in settings.allowed_base_urls:
            raise ProviderNotAllowedError(f"URL provider tidak diizinkan: {base_url}")
        return OpenAICompatibleProvider(
            name=credentials.provider,
            base_url=base_url,
            api_key=credentials.api_key.get_secret_value(),
            model=credentials.model,
            timeout_seconds=settings.llm_timeout_seconds,
            client=client,
        )

    if settings.llm_provider == "mock":
        return MockProvider()

    api_key = settings.deepseek_api_key.get_secret_value()
    if not api_key:
        raise ProviderNotConfiguredError("Request tanpa kredensial dan DEEPSEEK_API_KEY kosong")
    return OpenAICompatibleProvider(
        name="deepseek",
        base_url=settings.llm_base_url,
        api_key=api_key,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        client=client,
    )
