from collections.abc import Awaitable, Callable, Iterator
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import AppError, app_error_handler
from app.core.secrets import SecretConfigError, decrypt_secret, encrypt_secret
from app.entitlement.deps import require_feature
from app.entitlement.features import Feature
from app.main import app as fastapi_app
from app.models import AuditLog, Role, Tenant, TenantAiSetting
from app.services.ai_gateway import AiGatewayError, LlmCredentials, get_ai_gateway
from tests.conftest import AuthHeaders, MakeTenant, MakeUser

API_KEY = "sk-tenant-rahasia-1234"
URL = "/settings/ai"


class FakeGateway:
    """Pengganti ai-gateway: mencatat kredensial yang dikirim, bisa diatur gagal."""

    def __init__(self) -> None:
        self.calls: list[LlmCredentials] = []
        self.error: AiGatewayError | None = None
        self.usage = {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 1}

    async def chat(self, credentials: LlmCredentials, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(credentials)
        if self.error is not None:
            raise self.error
        return {"id": "x", "provider": credentials.provider, "usage": self.usage}


type UseSettings = Callable[..., Settings]
type AsUser = Callable[[Tenant, Role], Awaitable[dict[str, str]]]


@pytest.fixture
def gateway() -> Iterator[FakeGateway]:
    fake = FakeGateway()
    fastapi_app.dependency_overrides[get_ai_gateway] = lambda: fake
    yield fake
    fastapi_app.dependency_overrides.pop(get_ai_gateway, None)


@pytest.fixture
def use_settings() -> Iterator[UseSettings]:
    """Default test: AI_ENABLED=false. Test AI menyalakannya lewat override ini."""

    def _use(**overrides: Any) -> Settings:
        settings = get_settings().model_copy(update=overrides)
        fastapi_app.dependency_overrides[get_settings] = lambda: settings
        return settings

    yield _use
    fastapi_app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
async def as_user(make_user: MakeUser, auth_headers: AuthHeaders) -> AsUser:
    """Header untuk user sungguhan di DB (dibutuhkan /me)."""

    async def _as(tenant: Tenant, role: Role) -> dict[str, str]:
        user = await make_user(tenant, [role])
        return auth_headers(tenant, role, user_id=user.id)

    return _as


@pytest.fixture
async def setup(make_tenant: MakeTenant, as_user: AsUser) -> dict[str, Any]:
    tenant = await make_tenant()
    return {"tenant": tenant, "hr": await as_user(tenant, Role.HR_ADMIN)}


async def _patch(client: AsyncClient, hr: dict[str, str], **body: Any) -> Any:
    return await client.patch(URL, json=body, headers=hr)


async def _ready(client: AsyncClient, hr: dict[str, str]) -> dict[str, Any]:
    """Key diisi, persetujuan diberikan, tes koneksi sukses, AI Assistant aktif."""
    await _patch(client, hr, api_key=API_KEY, consent_accepted=True)
    test = await client.post(f"{URL}/test", headers=hr)
    assert test.json()["success"] is True, test.text
    response = await _patch(client, hr, enabled_features=["ai_assistant"])
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _me_features(client: AsyncClient, headers: dict[str, str]) -> list[str]:
    response = await client.get("/me", headers=headers)
    features: list[str] = response.json()["ai_features"]
    return features


# --- Alur utama --------------------------------------------------------------------------


async def test_default_settings(
    client: AsyncClient, setup: dict[str, Any], use_settings: UseSettings
) -> None:
    body = (await client.get(URL, headers=setup["hr"])).json()
    assert body["provider"] == "deepseek"
    assert body["model"] == "deepseek-flash"
    assert body["api_key_set"] is False
    assert body["status"] == "ai_disabled"  # AI_ENABLED=false di test
    assert body["active_features"] == []

    use_settings(ai_enabled=True)
    assert (await client.get(URL, headers=setup["hr"])).json()["status"] == "no_api_key"


async def test_full_flow_enables_features(
    client: AsyncClient,
    setup: dict[str, Any],
    gateway: FakeGateway,
    use_settings: UseSettings,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    settings = use_settings(ai_enabled=True)
    hr = setup["hr"]

    saved = await _patch(client, hr, api_key=API_KEY)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert (body["api_key_set"], body["api_key_hint"]) == (True, "1234")
    assert API_KEY not in saved.text
    assert body["status"] == "consent_required"

    not_yet = await _patch(client, hr, enabled_features=["ai_assistant"])
    assert not_yet.json()["detail"]["code"] == "ai_consent_required"
    await _patch(client, hr, consent_accepted=True)
    not_tested = await _patch(client, hr, enabled_features=["ai_assistant"])
    assert not_tested.json()["detail"]["code"] == "ai_key_not_verified"
    assert await _me_features(client, hr) == []

    test = await client.post(f"{URL}/test", headers=hr)
    assert test.status_code == 200
    assert test.json()["success"] is True
    assert gateway.calls[0].api_key == API_KEY  # didekripsi hanya untuk dikirim ke gateway
    assert gateway.calls[0].base_url == "https://api.deepseek.com"

    enabled = await _patch(client, hr, enabled_features=["ai_assistant", "ai_form_validation"])
    assert enabled.json()["status"] == "ready"
    assert enabled.json()["active_features"] == ["ai_assistant", "ai_form_validation"]
    assert await _me_features(client, hr) == ["ai_assistant", "ai_form_validation"]

    # Key tersimpan terenkripsi, dan tidak pernah muncul di audit.
    async with admin_sessionmaker() as s:
        stored = await s.scalar(
            select(TenantAiSetting.api_key_ciphertext).where(
                TenantAiSetting.tenant_id == setup["tenant"].id
            )
        )
        audit_text = await s.scalar(
            select(text("string_agg(after::text || coalesce(before::text, ''), ' ')"))
            .select_from(AuditLog)
            .where(AuditLog.tenant_id == setup["tenant"].id)
        )
    assert stored is not None
    assert API_KEY not in stored
    assert decrypt_secret(stored, settings) == API_KEY
    assert audit_text is not None
    assert API_KEY not in audit_text


async def test_changing_key_or_model_requires_new_test(
    client: AsyncClient, setup: dict[str, Any], gateway: FakeGateway, use_settings: UseSettings
) -> None:
    use_settings(ai_enabled=True)
    hr = setup["hr"]
    await _ready(client, hr)

    changed = await _patch(client, hr, model="deepseek-pro")
    body = changed.json()
    assert body["status"] == "key_not_verified"
    assert body["enabled_features"] == ["ai_assistant"]  # toggle tetap, tapi belum aktif
    assert body["active_features"] == []
    assert await _me_features(client, hr) == []

    await client.post(f"{URL}/test", headers=hr)
    assert await _me_features(client, hr) == ["ai_assistant"]

    await _patch(client, hr, api_key="sk-kunci-baru-9999")
    assert await _me_features(client, hr) == []

    cleared = await _patch(client, hr, clear_api_key=True)
    assert (cleared.json()["api_key_set"], cleared.json()["status"]) == (False, "no_api_key")


async def test_changing_provider_requires_model_and_new_consent(
    client: AsyncClient, setup: dict[str, Any], gateway: FakeGateway, use_settings: UseSettings
) -> None:
    use_settings(ai_enabled=True)
    hr = setup["hr"]
    await _ready(client, hr)

    missing_model = await _patch(client, hr, provider="openai")
    assert missing_model.json()["detail"]["code"] == "ai_model_required"

    switched = await _patch(client, hr, provider="openai", model="model-openai")
    body = switched.json()
    assert (body["provider"], body["base_url"]) == ("openai", "https://api.openai.com/v1")
    assert body["consent_accepted_at"] is None
    assert body["status"] == "consent_required"

    override = await _patch(client, hr, base_url="https://evil.example/v1")
    assert override.json()["detail"]["code"] == "ai_base_url_not_allowed"


async def test_custom_provider_only_with_operator_allowlist(
    client: AsyncClient, setup: dict[str, Any], gateway: FakeGateway, use_settings: UseSettings
) -> None:
    hr = setup["hr"]
    use_settings(ai_enabled=True)
    body = {"provider": "custom", "base_url": "http://169.254.169.254/latest", "model": "m"}
    blocked = await client.patch(URL, json=body, headers=hr)
    assert blocked.json()["detail"]["code"] == "ai_base_url_not_allowed"

    use_settings(ai_enabled=True, ai_allowed_base_urls="https://llm.internal.example/v1")
    allowed = await client.patch(
        URL,
        json=body | {"base_url": "https://llm.internal.example/v1/"},
        headers=hr,
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["base_url"] == "https://llm.internal.example/v1"
    assert allowed.json()["allowed_custom_base_urls"] == ["https://llm.internal.example/v1"]


async def test_failed_connection_test(
    client: AsyncClient, setup: dict[str, Any], gateway: FakeGateway, use_settings: UseSettings
) -> None:
    use_settings(ai_enabled=True)
    hr = setup["hr"]
    await _patch(client, hr, api_key=API_KEY, consent_accepted=True)
    gateway.error = AiGatewayError("provider_error", "Provider LLM gagal, gunakan mode ERP.")

    result = await client.post(f"{URL}/test", headers=hr)
    assert result.status_code == 200
    assert result.json()["success"] is False
    assert "Provider LLM gagal" in result.json()["message"]
    assert (await client.get(URL, headers=hr)).json()["status"] == "key_not_verified"

    no_key = await _patch(client, hr, clear_api_key=True)
    assert no_key.status_code == 200
    missing = await client.post(f"{URL}/test", headers=hr)
    assert missing.json()["detail"]["code"] == "ai_no_api_key"


async def test_monthly_limit_switches_to_erp_mode(
    client: AsyncClient, setup: dict[str, Any], gateway: FakeGateway, use_settings: UseSettings
) -> None:
    use_settings(ai_enabled=True)
    hr = setup["hr"]
    gateway.usage = {"input_tokens": 90, "cached_input_tokens": 40, "output_tokens": 20}
    await _ready(client, hr)  # 1 tes koneksi = 110 token

    await _patch(client, hr, monthly_token_limit=100)
    body = (await client.get(URL, headers=hr)).json()
    assert body["status"] == "limit_reached"
    assert body["active_features"] == []
    assert await _me_features(client, hr) == []

    usage = (await client.get(f"{URL}/usage", headers=hr)).json()
    assert (usage["calls"], usage["total_tokens"], usage["limit_reached"]) == (1, 110, True)
    assert usage["by_feature"][0]["feature"] == "connection_test"
    assert usage["by_feature"][0]["cached_input_tokens"] == 40

    unlimited = await client.patch(URL, json={"monthly_token_limit": None}, headers=hr)
    assert unlimited.json()["status"] == "ready"

    other_month = await client.get(f"{URL}/usage", params={"year": 2020, "month": 1}, headers=hr)
    assert other_month.json()["calls"] == 0


async def test_ai_disabled_on_server(
    client: AsyncClient, setup: dict[str, Any], gateway: FakeGateway, use_settings: UseSettings
) -> None:
    use_settings(ai_enabled=True)
    hr = setup["hr"]
    await _ready(client, hr)
    assert await _me_features(client, hr) == ["ai_assistant"]

    use_settings(ai_enabled=False)  # kill switch deployment
    assert await _me_features(client, hr) == []
    blocked = await client.post(f"{URL}/test", headers=hr)
    assert blocked.json()["detail"]["code"] == "ai_disabled"


async def test_missing_secret_key(
    client: AsyncClient, setup: dict[str, Any], use_settings: UseSettings
) -> None:
    use_settings(ai_enabled=True, ai_secret_key=None)
    response = await _patch(client, setup["hr"], api_key=API_KEY)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ai_secret_not_configured"


async def test_validation(client: AsyncClient, setup: dict[str, Any]) -> None:
    for body in (
        {"api_key": "pendek"},
        {"enabled_features": ["leave"]},
        {"monthly_token_limit": 0},
        {"provider": "provider_liar"},
        {"model": "ada spasi"},
    ):
        response = await client.patch(URL, json=body, headers=setup["hr"])
        assert response.status_code == 422, body


# --- Otorisasi dan isolasi ---------------------------------------------------------------


async def test_only_hr_and_tenant_isolation(
    client: AsyncClient,
    setup: dict[str, Any],
    gateway: FakeGateway,
    use_settings: UseSettings,
    make_tenant: MakeTenant,
    as_user: AsUser,
) -> None:
    use_settings(ai_enabled=True)
    await _ready(client, setup["hr"])

    for role in (Role.EMPLOYEE, Role.MANAGER):
        headers = await as_user(setup["tenant"], role)
        for method, url in (
            ("GET", URL),
            ("PATCH", URL),
            ("POST", f"{URL}/test"),
            ("GET", f"{URL}/usage"),
        ):
            body = {"model": "x"} if method == "PATCH" else None
            response = await client.request(method, url, json=body, headers=headers)
            assert response.status_code == 403, (role, method, url)
        # Karyawan tetap melihat fitur AI yang aktif lewat /me.
        assert await _me_features(client, headers) == ["ai_assistant"]

    other_hr = await as_user(await make_tenant(), Role.HR_ADMIN)
    other = (await client.get(URL, headers=other_hr)).json()
    assert (other["api_key_set"], other["status"]) == (False, "no_api_key")
    assert (await client.get(f"{URL}/usage", headers=other_hr)).json()["calls"] == 0
    assert await _me_features(client, other_hr) == []


# --- require_feature ---------------------------------------------------------------------


def _feature_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)

    @app.get("/ai", dependencies=[Depends(require_feature(Feature.AI_ASSISTANT))])
    async def ai_endpoint() -> dict[str, str]:
        return {"ok": "ai"}

    @app.get("/erp")
    async def erp_endpoint(
        _: Annotated[None, Depends(require_feature(Feature.LEAVE))],
    ) -> dict[str, str]:
        return {"ok": "erp"}

    return app


async def test_require_feature(
    client: AsyncClient, setup: dict[str, Any], gateway: FakeGateway, use_settings: UseSettings
) -> None:
    settings = use_settings(ai_enabled=True)
    app = _feature_app()
    app.dependency_overrides[get_settings] = lambda: settings
    hr = setup["hr"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as mini:
        blocked = await mini.get("/ai", headers=hr)
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "feature_not_enabled"
        assert (await mini.get("/erp", headers=hr)).status_code == 200

        await _ready(client, hr)
        assert (await mini.get("/ai", headers=hr)).json() == {"ok": "ai"}

        gateway.usage = {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 0}
        await client.post(f"{URL}/test", headers=hr)
        await _patch(client, hr, monthly_token_limit=500)
        limited = await mini.get("/ai", headers=hr)
        assert limited.status_code == 403
        assert limited.json()["detail"]["code"] == "ai_limit_reached"
        assert (await mini.get("/erp", headers=hr)).status_code == 200


# --- Enkripsi ----------------------------------------------------------------------------


def test_secret_rotation() -> None:
    old = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    new = "MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE="
    base = get_settings()

    def with_key(value: str) -> Settings:
        return base.model_copy(update={"ai_secret_key": SecretStr(value)})

    token = encrypt_secret(API_KEY, with_key(old))
    rotated = with_key(f"{new},{old}")
    assert decrypt_secret(token, rotated) == API_KEY
    assert decrypt_secret(encrypt_secret("x-key", rotated), rotated) == "x-key"

    with pytest.raises(SecretConfigError):
        encrypt_secret(API_KEY, with_key("bukan-key"))
