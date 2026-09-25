import httpx

from ai_gateway.config import Settings
from ai_gateway.providers.base import ChatProvider, ProviderError, ProviderNotConfiguredError
from ai_gateway.providers.mock import MockProvider
from ai_gateway.providers.openai_compatible import OpenAICompatibleProvider

__all__ = [
    "ChatProvider",
    "ProviderError",
    "ProviderNotConfiguredError",
    "build_provider",
]


def build_provider(settings: Settings, client: httpx.AsyncClient) -> ChatProvider:
    if settings.llm_provider == "mock":
        return MockProvider()

    api_key = settings.deepseek_api_key.get_secret_value()
    if not api_key:
        raise ProviderNotConfiguredError("DEEPSEEK_API_KEY belum diisi")
    return OpenAICompatibleProvider(
        name="deepseek",
        base_url=settings.llm_base_url,
        api_key=api_key,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        client=client,
    )
