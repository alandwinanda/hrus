from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.entitlement.deps import require_feature
from app.entitlement.features import AI_FEATURES, Feature


def _app_with(settings: Settings) -> FastAPI:
    app = FastAPI()

    @app.get("/ai", dependencies=[Depends(require_feature(Feature.AI_ASSISTANT))])
    async def ai_endpoint() -> dict[str, str]:
        return {"ok": "ai"}

    @app.get("/erp")
    async def erp_endpoint(
        _: Annotated[None, Depends(require_feature(Feature.LEAVE))],
    ) -> dict[str, str]:
        return {"ok": "erp"}

    app.dependency_overrides[get_settings] = lambda: settings
    return app


async def _get(app: FastAPI, path: str) -> tuple[int, dict[str, object]]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)
    return response.status_code, response.json()


async def test_ai_feature_blocked_when_ai_disabled() -> None:
    status, body = await _get(_app_with(Settings(ai_enabled=False)), "/ai")

    assert status == 403
    assert body["detail"]["code"] == "feature_not_enabled"  # type: ignore[index]


async def test_ai_feature_allowed_when_ai_enabled() -> None:
    status, body = await _get(_app_with(Settings(ai_enabled=True)), "/ai")

    assert status == 200
    assert body == {"ok": "ai"}


@pytest.mark.parametrize("ai_enabled", [True, False])
async def test_erp_feature_always_allowed(ai_enabled: bool) -> None:
    status, _ = await _get(_app_with(Settings(ai_enabled=ai_enabled)), "/erp")

    assert status == 200


def test_ai_features_are_all_prefixed() -> None:
    assert all(f.value.startswith("ai_") for f in AI_FEATURES)
    assert not any(f.value.startswith("ai_") for f in set(Feature) - AI_FEATURES)
