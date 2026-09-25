from typing import Any

import httpx
from pydantic import ValidationError

from ai_gateway.providers.base import ProviderError
from ai_gateway.schemas import ChatMessage, ChatRequest, ChatResponse, Usage


def _parse_usage(raw: dict[str, Any]) -> Usage:
    # DeepSeek: prompt_cache_hit_tokens. OpenAI: prompt_tokens_details.cached_tokens.
    cached = raw.get("prompt_cache_hit_tokens")
    if cached is None:
        cached = (raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return Usage(
        input_tokens=raw.get("prompt_tokens", 0),
        cached_input_tokens=cached or 0,
        output_tokens=raw.get("completion_tokens", 0),
    )


class OpenAICompatibleProvider:
    """Provider apa pun yang punya endpoint /chat/completions format OpenAI (DeepSeek, dll.)."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        client: httpx.AsyncClient,
    ) -> None:
        self.name = name
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._client = client

    async def chat(self, request: ChatRequest) -> ChatResponse:
        payload = {"model": self._model, **request.model_dump(exclude_none=True)}
        try:
            response = await self._client.post(
                self._url,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: {type(exc).__name__}") from exc

        if response.status_code >= 400:
            raise ProviderError(f"{self.name}: HTTP {response.status_code}")

        try:
            data = response.json()
            choice = data["choices"][0]
            return ChatResponse(
                id=data.get("id", ""),
                provider=self.name,
                model=data.get("model", self._model),
                message=ChatMessage.model_validate(choice["message"]),
                finish_reason=choice.get("finish_reason"),
                usage=_parse_usage(data.get("usage") or {}),
            )
        except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
            raise ProviderError(f"{self.name}: respons tidak valid") from exc
