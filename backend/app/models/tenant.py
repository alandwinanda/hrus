from sqlalchemy import String, true
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Perusahaan klien. Tabel induk semua data, tanpa RLS (role aplikasi hanya bisa SELECT)."""

    __tablename__ = "tenant"

    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), server_default="Asia/Jakarta")
    is_active: Mapped[bool] = mapped_column(server_default=true())
