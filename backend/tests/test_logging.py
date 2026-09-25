import io
import json
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import configure_logging, get_logger
from app.core.middleware import resolve_request_id
from app.core.tenant import TENANT_SETTING, set_tenant_context

CURRENT_TENANT = text("SELECT current_setting(:n, true)")


@pytest.fixture
def log_stream() -> io.StringIO:
    stream = io.StringIO()
    configure_logging(service="backend", stream=stream)
    return stream


def _records(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


async def test_request_log_is_json_with_request_and_tenant_id(
    client: AsyncClient, log_stream: io.StringIO
) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "req-123"})

    assert response.headers["x-request-id"] == "req-123"
    request_logs = [r for r in _records(log_stream) if r["event"] == "request"]
    assert request_logs[-1] | {"timestamp": None, "duration_ms": None} == {
        "service": "backend",
        "event": "request",
        "level": "info",
        "logger": "app.core.middleware",
        "module": "middleware",
        "timestamp": None,
        "duration_ms": None,
        "request_id": "req-123",
        "tenant_id": None,
        "method": "GET",
        "path": "/health",
        "status": 200,
    }


async def test_request_id_generated_when_missing(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert len(response.headers["x-request-id"]) == 32


@pytest.mark.parametrize("bad", ["", "a" * 129, "evil\nline", "spasi ada"])
def test_unsafe_request_id_replaced(bad: str) -> None:
    assert resolve_request_id(bad) != bad


def test_log_outside_request_still_has_context_keys(log_stream: io.StringIO) -> None:
    get_logger("test").info("hello")

    record = _records(log_stream)[-1]
    assert record["request_id"] is None
    assert record["tenant_id"] is None


async def test_set_tenant_context_is_transaction_local(
    session: AsyncSession, log_stream: io.StringIO
) -> None:
    tenant_id = uuid4()

    await set_tenant_context(session, tenant_id)
    get_logger("test").info("inside_tenant")

    current = await session.scalar(CURRENT_TENANT, {"n": TENANT_SETTING})
    assert current == str(tenant_id)
    assert _records(log_stream)[-1]["tenant_id"] == str(tenant_id)

    await session.rollback()
    await session.begin()
    after = await session.scalar(CURRENT_TENANT, {"n": TENANT_SETTING})
    assert after in (None, "")
