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
