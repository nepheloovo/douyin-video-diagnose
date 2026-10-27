"""Runtime settings — secrets only via env / .env, never hardcoded."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Empty string means unset. Effective OpenAI/DeepSeek defaults are applied
    # in app.services.llm so a DeepSeek-only key does not inherit the OpenAI host.
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = ""

    # Used only when openai_api_key is empty.
    deepseek_api_key: str = ""

    # Optional judge-stage override. All empty → judge reuses the primary client.
    judge_openai_api_key: str = ""
    judge_openai_base_url: str = ""
    judge_openai_model: str = ""
    judge_model: str = ""

    diagnose_fetch_timeout_sec: float = 8.0
    diagnose_job_ttl_sec: int = 3600

    @property
    def has_llm(self) -> bool:
        return bool(self.openai_api_key.strip() or self.deepseek_api_key.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
