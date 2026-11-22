from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://cachai:cachai_dev_password@postgres:5432/cachai"
    litellm_base_url: str = "http://litellm:4000"
    litellm_master_key: str = "sk-cachai-dev-master-key"
    internal_api_token: str = "change-me-internal-publish-token"

    daily_budget_usd: float = 5.0
    estimated_audit_cost_usd: float = 0.25
    audit_interval_minutes: int = 60
    demo_mode: bool = True

    sandbox_image: str = "python:3.11-slim"
    sandbox_mem_limit: str = "128m"
    sandbox_timeout_seconds: int = 15

    self_base_url: str = "http://127.0.0.1:8000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
