"""Template laporan (ADR 012). Menyimpan spesifikasi query terstruktur, bukan SQL.

Sumber data laporan adalah semantic views (v_employee, v_org_unit, v_leave_balance,
v_leave_request) yang dibuat di migrasi dan didaftarkan di app/reports/catalog.py.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ReportTemplate(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "report_template"
    __table_args__ = (
        Index("uq_report_template_tenant_id_name", "tenant_id", "name", unique=True),
        Index(
            "uq_report_template_idempotency",
            "tenant_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    query: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by_user_id: Mapped[UUID]
    updated_by_user_id: Mapped[UUID | None]
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
