from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr

from app.entitlement.features import AiFeature
from app.models import AiProvider

AiReadiness = Literal[
    "ready",
    "ai_disabled",  # AI_ENABLED=false di server
    "no_api_key",
    "consent_required",
    "key_not_verified",
    "limit_reached",
]


class AiSettingsRead(BaseModel):
    """Setting AI tenant. API key tidak pernah dikembalikan, hanya 4 karakter terakhir."""

    provider: AiProvider
    base_url: str
    model: str
    api_key_set: bool
    api_key_hint: str | None
    key_verified_at: datetime | None
    consent_accepted_at: datetime | None
    consent_accepted_by: UUID | None
    enabled_features: list[AiFeature]
    monthly_token_limit: int | None
    status: AiReadiness
    active_features: list[AiFeature] = Field(
        description="Fitur yang benar-benar bisa dipakai sekarang (toggle + semua syarat)."
    )
    allowed_custom_base_urls: list[str]


class AiSettingsUpdate(BaseModel):
    """Kirim hanya field yang diubah. monthly_token_limit=null berarti tanpa limit."""

    provider: AiProvider | None = None
    base_url: str | None = Field(default=None, max_length=255, description="Hanya untuk custom.")
    model: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:/@-]+$"
    )
    api_key: SecretStr | None = Field(default=None, min_length=8, max_length=500)
    clear_api_key: bool = False
    enabled_features: list[AiFeature] | None = None
    monthly_token_limit: int | None = Field(default=None, gt=0, le=10_000_000_000)
    consent_accepted: bool | None = Field(
        default=None,
        description=(
            "Persetujuan HR admin bahwa data (tanpa NIK, gaji, rekening; nama di-mask) diproses "
            "provider yang dipilih."
        ),
    )


class AiConnectionTestResult(BaseModel):
    success: bool
    provider: AiProvider
    model: str
    message: str
    key_verified_at: datetime | None


class AiUsageByFeature(BaseModel):
    feature: str
    calls: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int


class AiUsageSummary(BaseModel):
    month: str  # YYYY-MM, zona waktu tenant
    calls: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_tokens: int  # input + output, dasar perhitungan limit
    monthly_token_limit: int | None
    limit_reached: bool
    by_feature: list[AiUsageByFeature]
