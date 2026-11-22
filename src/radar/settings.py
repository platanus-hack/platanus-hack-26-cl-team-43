from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    github_token: str
    hf_token: str | None = None
    openrouter_api_key: str
    extractor_model: str
    judge_model: str
    daily_budget_usd: float = 20.0
    resend_api_key: str | None = None
    email_from: str
    twilio_sid: str | None = None
    twilio_token: str | None = None
    twilio_whatsapp_from: str | None = None
    allowed_emails: Annotated[list[str], NoDecode] = []
    secret_key: str
    base_url: str

    @field_validator("secret_key")
    @classmethod
    def _secret_len(cls, v):
        if len(v) < 32:
            raise ValueError("secret_key must be at least 32 characters")
        return v

    @field_validator("allowed_emails", mode="before")
    @classmethod
    def _split_emails(cls, v):
        if isinstance(v, str):
            return [e.strip() for e in v.split(",") if e.strip()]
        return v
