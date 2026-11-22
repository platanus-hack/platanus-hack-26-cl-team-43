from typing import Any, TypedDict


class AuditGraphState(TypedDict, total=False):
    trigger_source: str
    scraped_models: int
    scraped_prompts: int
    target_model: dict[str, Any] | None
    budget_approved: bool
    budget_remaining_usd: float
    budget_reason: str
    eval_results: list[dict[str, Any]]
    published_post_slug: str | None
    errors: list[str]
