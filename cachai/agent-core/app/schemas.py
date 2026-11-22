from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
import re

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class BlogPostCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=3, max_length=255)
    slug: str = Field(..., min_length=3, max_length=255)
    summary: str = Field(..., min_length=10, max_length=1000)
    markdown_body: str = Field(..., min_length=10)
    eval_id: int | None = Field(default=None, ge=1)
    status: Literal["draft", "published", "archived"] = "published"

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        if not SLUG_PATTERN.match(value):
            raise ValueError("slug must be lowercase alphanumeric with single hyphens")
        return value


class BlogPostResponse(BaseModel):
    id: int
    title: str
    slug: str
    summary: str
    markdown_body: str
    eval_id: int | None
    status: Literal["draft", "published", "archived"]
    published_at: datetime | None


class TriggerDemoResponse(BaseModel):
    status: str
    trigger_source: str
    target_model: dict | None
    budget_approved: bool
    budget_remaining_usd: float
    budget_reason: str
    evaluations_run: int
    published_post_slug: str | None
    errors: list[str]
