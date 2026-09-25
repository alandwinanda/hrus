from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Semua konfigurasi dari env var. File .env hanya dipakai untuk dev lokal."""

    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    service_name: str = "backend"
    deployment_mode: Literal["saas", "dedicated"] = "saas"
    ai_enabled: bool = False
    log_level: str = "INFO"

    # Lewat PgBouncer (transaction mode) untuk aplikasi.
    database_url: str = "postgresql+psycopg://hrus:hrus_dev_password@localhost:5432/hrus"
    # Langsung ke PostgreSQL untuk Alembic. Kosong berarti pakai database_url.
    migration_database_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"

    ready_timeout_seconds: float = Field(default=2.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
