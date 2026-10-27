"""Runtime settings — secrets only via env / .env, never hardcoded."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    diagnose_fetch_timeout_sec: float = 8.0
    diagnose_job_ttl_sec: int = 3600

    @property
    def has_llm(self) -> bool:
        return bool(self.openai_api_key.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
