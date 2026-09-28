"""Client ke ai-gateway. Satu-satunya jalan Core API memanggil LLM (ADR 004, ADR 011).

API key tenant dikirim per request di body (jaringan internal) dan tidak pernah di-log.
"""

import contextlib
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class LlmCredentials:
    provider: str
    base_url: str
    model: str
    api_key: str

    def __repr__(self) -> str:  # jangan pernah tampilkan key di log/traceback
        return f"LlmCredentials(provider={self.provider!r}, model={self.model!r})"


class AiGatewayError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AiGateway(Protocol):
    async def chat(
        self, credentials: LlmCredentials, payload: dict[str, Any]
    ) -> dict[str, Any]: ...


class HttpAiGateway:
    def __init__(self, settings: Settings) -> None:
        self._url = f"{settings.ai_gateway_url.rstrip('/')}/v1/chat"
        self._timeout = settings.ai_gateway_timeout_seconds

    async def chat(self, credentials: LlmCredentials, payload: dict[str, Any]) -> dict[str, Any]:
        body = payload | {
            "credentials": {
                "provider": credentials.provider,
                "base_url": credentials.base_url,
                "model": credentials.model,
                "api_key": credentials.api_key,
            }
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            raise AiGatewayError("gateway_unreachable", "AI gateway tidak bisa dihubungi.") from exc
        if response.status_code >= 400:
            raise _gateway_error(response)
        result: dict[str, Any] = response.json()
        return result


def _gateway_error(response: httpx.Response) -> AiGatewayError:
    """Ambil code/message dari body error gateway. Body 422 FastAPI berupa list, bukan dict."""
    detail: object = None
    with contextlib.suppress(ValueError, AttributeError):
        detail = response.json().get("detail")
    if isinstance(detail, dict):
        return AiGatewayError(
            str(detail.get("code", "gateway_error")),
            str(detail.get("message", f"AI gateway error HTTP {response.status_code}.")),
        )
    return AiGatewayError("gateway_error", f"AI gateway error HTTP {response.status_code}.")


def get_ai_gateway() -> AiGateway:
    """Dependency FastAPI, di-override di test."""
    return HttpAiGateway(get_settings())
