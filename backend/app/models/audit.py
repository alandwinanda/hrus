from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, UUIDPrimaryKeyMixin


class AuditLog(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """Catatan perubahan data. Append-only: role aplikasi hanya punya SELECT dan INSERT.

    Partisi bulanan (pg_partman) menyusul saat volume mulai besar, lihat SPEC.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index(
            "ix_audit_log_tenant_id_entity",
            "tenant_id",
            "entity_type",
            "entity_id",
            "created_at",
        ),
        Index("ix_audit_log_created_at_brin", "created_at", postgresql_using="brin"),
    )

    actor_user_id: Mapped[UUID | None]
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
