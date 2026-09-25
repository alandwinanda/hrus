import os
from collections.abc import AsyncIterator, Callable

os.environ["AI_ENABLED"] = "false"
os.environ["DEEPSEEK_API_KEY"] = ""

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from ai_gateway.api import get_http_client
from ai_gateway.config import Settings, get_settings
from ai_gateway.main import app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
def use_settings() -> Callable[..., Settings]:
    def _use(**overrides: object) -> Settings:
        settings = Settings.model_validate(overrides)
        app.dependency_overrides[get_settings] = lambda: settings
        return settings

    return _use


@pytest.fixture
def use_upstream() -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    """Ganti HTTP client provider dengan transport palsu, tidak ada request keluar."""

    def _use(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.dependency_overrides[get_http_client] = lambda: upstream

    return _use
