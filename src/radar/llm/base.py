from dataclasses import dataclass
from typing import Protocol


@dataclass
class ChatResult:
    text: str
    cost_usd: float
    latency_ms: int
    provider_blocked: bool  # provider-side moderation refusal (counts as "refused")


class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class LLM(Protocol):
    def chat(self, model: str, user: str, *, system: str | None = None,
             json_schema: dict | None = None, max_tokens: int = 600) -> ChatResult: ...
