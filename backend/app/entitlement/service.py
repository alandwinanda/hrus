"""Fitur mana yang aktif untuk tenant (ADR 011). Tanpa paket: fitur ERP selalu aktif, fitur AI
aktif kalau tenant menyalakannya dan semua syarat terpenuhi."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.entitlement.features import AI_FEATURES, AiFeature, Feature
from app.models import TenantAiSetting
from app.schemas.ai import AiReadiness
from app.services import ai_usage


@dataclass(frozen=True, slots=True)
class AiStatus:
    status: AiReadiness
    active_features: frozenset[AiFeature]


def readiness(setting: TenantAiSetting | None, settings: Settings) -> AiReadiness:
    """Syarat statis (tanpa limit). Urutan = langkah yang harus dilakukan HR."""
    if not settings.ai_enabled:
        return "ai_disabled"
    if setting is None or not setting.api_key_ciphertext:
        return "no_api_key"
    if setting.consent_accepted_at is None:
        return "consent_required"
    if setting.key_verified_at is None:
        return "key_not_verified"
    return "ready"


async def ai_status(
    session: AsyncSession,
    tenant_id: UUID,
    settings: Settings,
    setting: TenantAiSetting | None = None,
) -> AiStatus:
    if setting is None:
        setting = await session.scalar(
            select(TenantAiSetting).where(TenantAiSetting.tenant_id == tenant_id)
        )
    status = readiness(setting, settings)
    if status != "ready" or setting is None:
        return AiStatus(status, frozenset())
    if setting.monthly_token_limit is not None:
        month = await ai_usage.current_month(session, tenant_id)
        if await ai_usage.month_tokens(session, tenant_id, month) >= setting.monthly_token_limit:
            return AiStatus("limit_reached", frozenset())
    features = frozenset(AiFeature(f) for f in setting.enabled_features if f in set(AiFeature))
    return AiStatus("ready", features)


async def is_feature_enabled(
    session: AsyncSession, tenant_id: UUID, feature: Feature, settings: Settings
) -> bool:
    if feature not in AI_FEATURES:
        return True
    status = await ai_status(session, tenant_id, settings)
    return AiFeature(feature.value) in status.active_features
