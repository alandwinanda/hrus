"""Setting AI per tenant (BYOK) dan pencatatan pemakaian token (ADR 011)."""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class AiProvider(StrEnum):
    DEEPSEEK = "deepseek"
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    CUSTOM = "custom"  # hanya URL di allowlist operator (AI_ALLOWED_BASE_URLS)


def _in(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(m.value) for m in enum)})"


class TenantAiSetting(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Satu baris per tenant. API key disimpan terenkripsi, tidak pernah dikembalikan API."""

    __tablename__ = "tenant_ai_setting"
    __table_args__ = (
        Index("uq_tenant_ai_setting_tenant_id", "tenant_id", unique=True),
        CheckConstraint(_in("provider", AiProvider), name="provider_valid"),
        CheckConstraint(
            "monthly_token_limit IS NULL OR monthly_token_limit > 0", name="limit_positive"
        ),
    )

    provider: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str] = mapped_column(String(255))
    model: Mapped[str] = mapped_column(String(100))
    api_key_ciphertext: Mapped[str | None] = mapped_column(Text)
    api_key_hint: Mapped[str | None] = mapped_column(String(8))
    key_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled_features: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), server_default=text("'{}'")
    )
    monthly_token_limit: Mapped[int | None] = mapped_column(BigInteger)
    consent_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_accepted_by: Mapped[UUID | None]
    updated_by_user_id: Mapped[UUID | None]


class AiUsage(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """Satu baris per panggilan LLM. Append-only: role aplikasi hanya SELECT dan INSERT.

    Partisi bulanan menyusul saat volume besar (SPEC: performa jangka panjang).
    """

    __tablename__ = "ai_usage"
    __table_args__ = (Index("ix_ai_usage_tenant_id_created", "tenant_id", "created_at"),)

    feature: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    user_id: Mapped[UUID | None]
    input_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    cached_input_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    success: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiUsageMonthly(TenantMixin, Base):
    """Counter per tenant/bulan/fitur, di-update di transaksi yang sama dengan ai_usage, supaya
    cek limit bulanan cukup membaca beberapa baris."""

    __tablename__ = "ai_usage_monthly"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "month", "feature"),)

    month: Mapped[date]
    feature: Mapped[str] = mapped_column(String(64))
    calls: Mapped[int] = mapped_column(server_default=text("0"))
    input_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
