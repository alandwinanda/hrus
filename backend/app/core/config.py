from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Semua konfigurasi dari env var. File .env hanya dipakai untuk dev lokal."""

    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    service_name: str = "backend"
    app_env: Literal["development", "test", "production"] = "development"
    deployment_mode: Literal["saas", "dedicated"] = "saas"
    ai_enabled: bool = False
    log_level: str = "INFO"

    # Lewat PgBouncer (transaction mode) untuk aplikasi, memakai user DB non-superuser.
    database_url: str = "postgresql+psycopg://hrus_app_user:hrus_app_password@localhost:5432/hrus"
    # Langsung ke PostgreSQL sebagai owner/superuser, hanya untuk migrasi dan CLI admin.
    migration_database_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"

    # User login aplikasi di PostgreSQL, anggota role grup hrus_app (tanpa BYPASSRLS).
    app_db_user: str = "hrus_app_user"
    app_db_password: SecretStr | None = None

    # Auth. JWT_SECRET wajib diisi (minimal 32 karakter) sebelum endpoint auth dipakai.
    jwt_secret: SecretStr | None = None
    jwt_issuer: str = "hrus"
    access_token_ttl_seconds: int = Field(default=900, gt=0)
    refresh_token_ttl_days: int = Field(default=7, gt=0)
    refresh_cookie_name: str = "hrus_refresh"
    # Path cookie mengikuti URL yang dilihat browser (nginx menambahkan prefix /api).
    refresh_cookie_path: str = "/api/auth"
    cookie_secure: bool = True
    login_max_attempts: int = Field(default=5, gt=0)
    login_lockout_seconds: int = Field(default=900, gt=0)

    ready_timeout_seconds: float = Field(default=2.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
