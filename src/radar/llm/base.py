from dataclasses import dataclass
from typing import Literal, Protocol

ErrorKind = Literal["model", "auth", "other"]


@dataclass
class ChatResult:
    text: str
    cost_usd: float
    latency_ms: int
    provider_blocked: bool  # provider-side moderation refusal (counts as "refused")
    finish_reason: str | None = None


class LLMError(Exception):
    """kind: "model" (model id gone/invalid), "auth" (bad key / no credits, global)
    or "other"."""

    def __init__(self, message: str, *, retryable: bool, kind: ErrorKind = "other"):
        super().__init__(message)
        self.retryable = retryable
        self.kind = kind


class LLM(Protocol):
    def chat(self, model: str, user: str, *, system: str | None = None,
             json_schema: dict | None = None, max_tokens: int = 600) -> ChatResult: ...
