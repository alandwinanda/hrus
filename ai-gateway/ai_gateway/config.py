from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    service_name: str = "ai-gateway"
    log_level: str = "INFO"
    ai_enabled: bool = False

    # deepseek: endpoint format OpenAI (/chat/completions). mock: untuk test dan dev tanpa API key.
    llm_provider: Literal["deepseek", "mock"] = "deepseek"
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    deepseek_api_key: SecretStr = SecretStr("")


@lru_cache
def get_settings() -> Settings:
    return Settings()
