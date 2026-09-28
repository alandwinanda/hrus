from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Provider bawaan untuk API key milik tenant (ADR 011). Harus sama dengan daftar di Core API.
PROVIDER_BASE_URLS = (
    "https://api.deepseek.com",
    "https://api.openai.com/v1",
    "https://openrouter.ai/api/v1",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    service_name: str = "ai-gateway"
    log_level: str = "INFO"
    ai_enabled: bool = False

    # Dipakai kalau request tidak membawa kredensial tenant (deployment dedicated dengan key
    # yang dikelola operator). deepseek: format OpenAI. mock: test dan dev tanpa API key.
    llm_provider: Literal["deepseek", "mock"] = "deepseek"
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    deepseek_api_key: SecretStr = SecretStr("")

    # URL tambahan (dipisah koma) yang boleh dipakai kredensial tenant, misal LLM privat.
    # Selain ini dan provider bawaan, request ditolak supaya gateway tidak jadi alat SSRF.
    ai_allowed_base_urls: str = ""

    @property
    def allowed_base_urls(self) -> frozenset[str]:
        extra = (url.strip().rstrip("/") for url in self.ai_allowed_base_urls.split(","))
        return frozenset(PROVIDER_BASE_URLS) | {url for url in extra if url}


@lru_cache
def get_settings() -> Settings:
    return Settings()
