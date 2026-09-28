"""Setting AI tenant (BYOK, ADR 011): provider, model, API key, toggle fitur, limit, persetujuan.

Hanya HR admin. API key dienkripsi sebelum disimpan dan tidak pernah dikembalikan atau diaudit.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import RuleViolationError
from app.core.secrets import (
    SecretConfigError,
    SecretDecryptError,
    decrypt_secret,
    encrypt_secret,
    secret_hint,
)
from app.core.security import AccessClaims
from app.entitlement.features import AiFeature
from app.entitlement.service import ai_status, readiness
from app.models import AiProvider, TenantAiSetting
from app.schemas.ai import AiConnectionTestResult, AiSettingsRead, AiSettingsUpdate, AiUsageSummary
from app.services import ai_usage
from app.services.ai_gateway import AiGateway, AiGatewayError, LlmCredentials
from app.services.audit import record_audit

# Provider bawaan: URL tetap, tenant tidak bisa mengarahkan server ke URL lain (SSRF).
PROVIDER_BASE_URLS: dict[AiProvider, str] = {
    AiProvider.DEEPSEEK: "https://api.deepseek.com",
    AiProvider.OPENAI: "https://api.openai.com/v1",
    AiProvider.OPENROUTER: "https://openrouter.ai/api/v1",
}
DEFAULT_PROVIDER = AiProvider.DEEPSEEK
DEFAULT_MODEL = "deepseek-flash"


def _config_error(exc: SecretConfigError) -> RuleViolationError:
    return RuleViolationError(
        f"Penyimpanan API key belum dikonfigurasi di server: {exc}", code="ai_secret_not_configured"
    )


async def _load(
    session: AsyncSession, tenant_id: UUID, *, lock: bool = False
) -> TenantAiSetting | None:
    stmt = select(TenantAiSetting).where(TenantAiSetting.tenant_id == tenant_id)
    if lock:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def _view(
    session: AsyncSession, tenant_id: UUID, setting: TenantAiSetting | None, settings: Settings
) -> AiSettingsRead:
    status = await ai_status(session, tenant_id, settings, setting)
    return AiSettingsRead(
        provider=AiProvider(setting.provider) if setting else DEFAULT_PROVIDER,
        base_url=setting.base_url if setting else PROVIDER_BASE_URLS[DEFAULT_PROVIDER],
        model=setting.model if setting else DEFAULT_MODEL,
        api_key_set=bool(setting and setting.api_key_ciphertext),
        api_key_hint=setting.api_key_hint if setting else None,
        key_verified_at=setting.key_verified_at if setting else None,
        consent_accepted_at=setting.consent_accepted_at if setting else None,
        consent_accepted_by=setting.consent_accepted_by if setting else None,
        enabled_features=sorted(
            AiFeature(f) for f in (setting.enabled_features if setting else [])
        ),
        monthly_token_limit=setting.monthly_token_limit if setting else None,
        status=status.status,
        active_features=sorted(status.active_features),
        allowed_custom_base_urls=settings.ai_custom_base_urls,
    )


async def get_ai_settings(
    session: AsyncSession, tenant_id: UUID, settings: Settings
) -> AiSettingsRead:
    return await _view(session, tenant_id, await _load(session, tenant_id), settings)


def _audit_view(setting: TenantAiSetting) -> dict[str, Any]:
    """Isi audit tanpa secret: key hanya diwakili hint."""
    return {
        "provider": setting.provider,
        "base_url": setting.base_url,
        "model": setting.model,
        "api_key_hint": setting.api_key_hint,
        "key_verified": setting.key_verified_at is not None,
        "consent_accepted": setting.consent_accepted_at is not None,
        "enabled_features": sorted(setting.enabled_features),
        "monthly_token_limit": setting.monthly_token_limit,
    }


def _resolve_base_url(provider: AiProvider, base_url: str | None, settings: Settings) -> str:
    if provider != AiProvider.CUSTOM:
        if base_url is not None and base_url.rstrip("/") != PROVIDER_BASE_URLS[provider]:
            raise RuleViolationError(
                "URL hanya bisa diatur untuk provider custom.", code="ai_base_url_not_allowed"
            )
        return PROVIDER_BASE_URLS[provider]
    url = (base_url or "").rstrip("/")
    if url not in settings.ai_custom_base_urls:
        raise RuleViolationError(
            "URL provider custom harus terdaftar di allowlist server (AI_ALLOWED_BASE_URLS).",
            code="ai_base_url_not_allowed",
        )
    return url


async def update_ai_settings(
    session: AsyncSession, actor: AccessClaims, data: AiSettingsUpdate, settings: Settings
) -> AiSettingsRead:
    tenant_id = actor.tenant_id
    setting = await _load(session, tenant_id, lock=True)
    if setting is None:
        setting = TenantAiSetting(
            tenant_id=tenant_id,
            provider=DEFAULT_PROVIDER,
            base_url=PROVIDER_BASE_URLS[DEFAULT_PROVIDER],
            model=DEFAULT_MODEL,
            enabled_features=[],
        )
        session.add(setting)
        before = None
    else:
        before = _audit_view(setting)
    fields = data.model_fields_set

    provider = data.provider or AiProvider(setting.provider)
    provider_changed = provider != setting.provider
    if provider_changed or data.base_url is not None:
        base_url = _resolve_base_url(provider, data.base_url, settings)
        if provider_changed:
            # Data akan dikirim ke pihak lain: persetujuan dan tes koneksi diulang.
            setting.consent_accepted_at = None
            setting.consent_accepted_by = None
            setting.key_verified_at = None
            if data.model is None:
                if provider != AiProvider.DEEPSEEK:
                    raise RuleViolationError(
                        "Pilih model untuk provider ini.", code="ai_model_required"
                    )
                setting.model = DEFAULT_MODEL
        if base_url != setting.base_url:
            setting.key_verified_at = None
        setting.provider = provider
        setting.base_url = base_url
    if data.model is not None and data.model != setting.model:
        setting.model = data.model
        setting.key_verified_at = None

    if data.clear_api_key:
        setting.api_key_ciphertext = None
        setting.api_key_hint = None
        setting.key_verified_at = None
    if data.api_key is not None:
        raw_key = data.api_key.get_secret_value().strip()
        try:
            setting.api_key_ciphertext = encrypt_secret(raw_key, settings)
        except SecretConfigError as exc:
            raise _config_error(exc) from exc
        setting.api_key_hint = secret_hint(raw_key)
        setting.key_verified_at = None

    if "monthly_token_limit" in fields:
        setting.monthly_token_limit = data.monthly_token_limit
    if data.consent_accepted is True and setting.consent_accepted_at is None:
        setting.consent_accepted_at = datetime.now(UTC)
        setting.consent_accepted_by = actor.user_id
    elif data.consent_accepted is False:
        setting.consent_accepted_at = None
        setting.consent_accepted_by = None

    if data.enabled_features is not None:
        wanted = sorted({str(f) for f in data.enabled_features})
        newly_enabled = set(wanted) - set(setting.enabled_features)
        if newly_enabled:
            # AI_ENABLED tidak ikut dicek di sini: HR boleh menyiapkan setting sebelum server
            # menyalakan AI.
            state = readiness(setting, settings.model_copy(update={"ai_enabled": True}))
            if state in _NOT_READY:
                code, message = _NOT_READY[state]
                raise RuleViolationError(message, code=code)
        setting.enabled_features = wanted
    setting.updated_by_user_id = actor.user_id

    await session.flush()
    await session.refresh(setting)
    after = _audit_view(setting)
    if after != before or data.api_key is not None:
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor.user_id,
            action="ai_setting.update",
            entity_type="tenant_ai_setting",
            entity_id=setting.id,
            before=before,
            after=after | {"api_key_changed": data.api_key is not None or data.clear_api_key},
        )
    return await _view(session, tenant_id, setting, settings)


_NOT_READY: dict[str, tuple[str, str]] = {
    "no_api_key": ("ai_no_api_key", "Isi API key dulu sebelum mengaktifkan fitur AI."),
    "consent_required": (
        "ai_consent_required",
        "Setujui pemrosesan data oleh provider dulu sebelum mengaktifkan fitur AI.",
    ),
    "key_not_verified": (
        "ai_key_not_verified",
        "Tes koneksi API key dulu sebelum mengaktifkan fitur AI.",
    ),
}


async def credentials_for(
    session: AsyncSession, tenant_id: UUID, settings: Settings
) -> LlmCredentials | None:
    """Kredensial LLM tenant (key didekripsi) untuk dikirim ke ai-gateway."""
    setting = await _load(session, tenant_id)
    return None if setting is None else _credentials(setting, settings)


def _credentials(setting: TenantAiSetting, settings: Settings) -> LlmCredentials | None:
    if not setting.api_key_ciphertext:
        return None
    try:
        api_key = decrypt_secret(setting.api_key_ciphertext, settings)
    except SecretConfigError as exc:
        raise _config_error(exc) from exc
    except SecretDecryptError as exc:
        raise RuleViolationError(
            "API key tersimpan tidak bisa dibuka. Masukkan ulang API key.",
            code="ai_key_unreadable",
        ) from exc
    return LlmCredentials(
        provider=setting.provider, base_url=setting.base_url, model=setting.model, api_key=api_key
    )


async def test_connection(
    session: AsyncSession, actor: AccessClaims, settings: Settings, gateway: AiGateway
) -> AiConnectionTestResult:
    """Panggilan kecil ke provider lewat ai-gateway. Sukses = key boleh dipakai fitur AI."""
    if not settings.ai_enabled:
        raise RuleViolationError(
            "AI dimatikan di server ini (AI_ENABLED=false).", code="ai_disabled"
        )
    setting = await _load(session, actor.tenant_id)
    credentials = None if setting is None else _credentials(setting, settings)
    if setting is None or credentials is None:
        raise RuleViolationError("Isi API key dulu.", code="ai_no_api_key")
    tested = (setting.api_key_ciphertext, setting.base_url, setting.model)

    payload = {"messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    try:
        response = await gateway.chat(credentials, payload)
    except AiGatewayError as exc:
        setting = await _load(session, actor.tenant_id, lock=True)
        if setting is None:  # pragma: no cover - setting tidak pernah dihapus
            raise RuleViolationError("Isi API key dulu.", code="ai_no_api_key") from exc
        setting.key_verified_at = None
        await ai_usage.record_usage(
            session,
            actor.tenant_id,
            feature=ai_usage.CONNECTION_TEST,
            provider=setting.provider,
            model=setting.model,
            user_id=actor.user_id,
            usage=None,
            success=False,
        )
        return AiConnectionTestResult(
            success=False,
            provider=AiProvider(setting.provider),
            model=setting.model,
            message=f"Koneksi gagal: {exc.message}",
            key_verified_at=None,
        )

    # Kunci baris setelah panggilan jaringan, dan hanya tandai terverifikasi kalau key, URL,
    # dan model belum diubah orang lain selama tes berjalan.
    setting = await _load(session, actor.tenant_id, lock=True)
    if setting is None or (setting.api_key_ciphertext, setting.base_url, setting.model) != tested:
        raise RuleViolationError(
            "Setting AI berubah saat tes berjalan. Ulangi tes koneksi.", code="ai_setting_changed"
        )
    setting.key_verified_at = datetime.now(UTC)
    await ai_usage.record_usage(
        session,
        actor.tenant_id,
        feature=ai_usage.CONNECTION_TEST,
        provider=setting.provider,
        model=setting.model,
        user_id=actor.user_id,
        usage=response.get("usage"),
        success=True,
    )
    await record_audit(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="ai_setting.verified",
        entity_type="tenant_ai_setting",
        entity_id=setting.id,
        after={"provider": setting.provider, "model": setting.model},
    )
    return AiConnectionTestResult(
        success=True,
        provider=AiProvider(setting.provider),
        model=setting.model,
        message="Koneksi berhasil.",
        key_verified_at=setting.key_verified_at,
    )


async def get_usage(
    session: AsyncSession, tenant_id: UUID, *, year: int | None, month: int | None
) -> AiUsageSummary:
    setting = await _load(session, tenant_id)
    period = None
    if year is not None and month is not None:
        period = datetime(year, month, 1).date()
    return await ai_usage.usage_summary(
        session, tenant_id, month=period, limit=setting.monthly_token_limit if setting else None
    )
