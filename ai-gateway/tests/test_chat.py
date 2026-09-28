import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from ai_gateway.config import Settings

CHAT_BODY = {"messages": [{"role": "user", "content": "Sisa cuti saya berapa?"}]}

DEEPSEEK_RESPONSE = {
    "id": "chatcmpl-1",
    "model": "deepseek-flash",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_leave_balance", "arguments": "{}"},
                    }
                ],
            },
        }
    ],
    "usage": {
        "prompt_tokens": 1200,
        "completion_tokens": 30,
        "prompt_cache_hit_tokens": 800,
        "prompt_cache_miss_tokens": 400,
    },
}

UseSettings = Callable[..., Settings]
UseUpstream = Callable[[Callable[[httpx.Request], httpx.Response]], None]


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ai-gateway"}


async def test_ready_reports_ai_state(client: AsyncClient, use_settings: UseSettings) -> None:
    use_settings(ai_enabled=False, llm_provider="mock")

    response = await client.get("/ready")

    assert response.json() == {"status": "ready", "ai_enabled": False, "provider": "mock"}


async def test_chat_returns_503_when_ai_disabled(
    client: AsyncClient, use_settings: UseSettings
) -> None:
    use_settings(ai_enabled=False, llm_provider="mock")

    response = await client.post("/v1/chat", json=CHAT_BODY)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ai_disabled"


async def test_chat_with_mock_provider(client: AsyncClient, use_settings: UseSettings) -> None:
    use_settings(ai_enabled=True, llm_provider="mock")

    response = await client.post("/v1/chat", json=CHAT_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "mock"
    assert body["message"] == {
        "role": "assistant",
        "content": "[mock] Sisa cuti saya berapa?",
        "tool_calls": None,
        "tool_call_id": None,
        "name": None,
    }


async def test_chat_forwards_to_deepseek_in_openai_format(
    client: AsyncClient, use_settings: UseSettings, use_upstream: UseUpstream
) -> None:
    use_settings(
        ai_enabled=True,
        llm_provider="deepseek",
        llm_base_url="https://llm.example/",
        llm_model="deepseek-flash",
        deepseek_api_key="sk-test",
    )
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=DEEPSEEK_RESPONSE)

    use_upstream(handler)
    tools = [{"type": "function", "function": {"name": "get_leave_balance", "parameters": {}}}]

    response = await client.post("/v1/chat", json={**CHAT_BODY, "tools": tools})

    assert response.status_code == 200
    assert seen["url"] == "https://llm.example/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"] == {"model": "deepseek-flash", **CHAT_BODY, "tools": tools}
    body = response.json()
    assert body["provider"] == "deepseek"
    assert body["finish_reason"] == "tool_calls"
    assert body["message"]["tool_calls"][0]["function"]["name"] == "get_leave_balance"
    assert body["usage"] == {"input_tokens": 1200, "cached_input_tokens": 800, "output_tokens": 30}


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx.Response(500, json={"error": "boom"}),
        lambda _: httpx.Response(200, json={"unexpected": True}),
        lambda _: httpx.Response(200, content=b"bukan json"),
    ],
)
async def test_chat_returns_502_on_bad_provider_response(
    client: AsyncClient,
    use_settings: UseSettings,
    use_upstream: UseUpstream,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    use_settings(ai_enabled=True, llm_provider="deepseek", deepseek_api_key="sk-test")
    use_upstream(handler)

    response = await client.post("/v1/chat", json=CHAT_BODY)

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "provider_error"


async def test_chat_returns_502_on_network_error(
    client: AsyncClient, use_settings: UseSettings, use_upstream: UseUpstream
) -> None:
    use_settings(ai_enabled=True, llm_provider="deepseek", deepseek_api_key="sk-test")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    use_upstream(handler)

    response = await client.post("/v1/chat", json=CHAT_BODY)

    assert response.status_code == 502


async def test_chat_returns_503_when_api_key_missing(
    client: AsyncClient, use_settings: UseSettings
) -> None:
    use_settings(ai_enabled=True, llm_provider="deepseek", deepseek_api_key="")

    response = await client.post("/v1/chat", json=CHAT_BODY)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ai_not_configured"


async def test_chat_rejects_empty_messages(client: AsyncClient, use_settings: UseSettings) -> None:
    use_settings(ai_enabled=True, llm_provider="mock")

    response = await client.post("/v1/chat", json={"messages": []})

    assert response.status_code == 422


TENANT_CREDENTIALS = {
    "provider": "openai",
    "base_url": "https://api.openai.com/v1/",
    "model": "model-tenant",
    "api_key": "sk-tenant-rahasia",
}


async def test_chat_uses_tenant_credentials(
    client: AsyncClient, use_settings: UseSettings, use_upstream: UseUpstream
) -> None:
    """BYOK: key dan model dari request, bukan env. Kredensial tidak ikut diteruskan di body."""
    use_settings(ai_enabled=True, llm_provider="deepseek", deepseek_api_key="sk-operator")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=DEEPSEEK_RESPONSE)

    use_upstream(handler)

    response = await client.post("/v1/chat", json={**CHAT_BODY, "credentials": TENANT_CREDENTIALS})

    assert response.status_code == 200, response.text
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-tenant-rahasia"
    assert seen["body"] == {"model": "model-tenant", **CHAT_BODY}
    assert response.json()["provider"] == "openai"
    assert "sk-tenant-rahasia" not in response.text


@pytest.mark.parametrize(
    "base_url",
    ["http://169.254.169.254/latest", "http://backend-1:8000", "https://evil.example/v1"],
)
async def test_chat_rejects_base_url_outside_allowlist(
    client: AsyncClient, use_settings: UseSettings, use_upstream: UseUpstream, base_url: str
) -> None:
    use_settings(ai_enabled=True)
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(200, json=DEEPSEEK_RESPONSE)

    use_upstream(handler)
    credentials = TENANT_CREDENTIALS | {"base_url": base_url}

    response = await client.post("/v1/chat", json={**CHAT_BODY, "credentials": credentials})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "base_url_not_allowed"
    assert called == []


async def test_operator_can_allow_private_llm(
    client: AsyncClient, use_settings: UseSettings, use_upstream: UseUpstream
) -> None:
    use_settings(ai_enabled=True, ai_allowed_base_urls="https://llm.internal.example/v1")
    use_upstream(lambda _: httpx.Response(200, json=DEEPSEEK_RESPONSE))
    credentials = TENANT_CREDENTIALS | {"base_url": "https://llm.internal.example/v1"}

    response = await client.post("/v1/chat", json={**CHAT_BODY, "credentials": credentials})

    assert response.status_code == 200


async def test_tenant_credentials_still_need_ai_enabled(
    client: AsyncClient, use_settings: UseSettings
) -> None:
    use_settings(ai_enabled=False)

    response = await client.post("/v1/chat", json={**CHAT_BODY, "credentials": TENANT_CREDENTIALS})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ai_disabled"
