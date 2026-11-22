import math
from collections.abc import Callable
from typing import Any

from radar.llm.base import ChatResult

DIM = 384


class FakeLLM:
    def __init__(self, responses: dict[str, Any] | Callable[..., Any] | None = None):
        self.responses = responses if responses is not None else {}
        self.calls: list[dict[str, Any]] = []

    def chat(self, model: str, user: str, *, system: str | None = None,
             json_schema: dict | None = None, max_tokens: int = 600) -> ChatResult:
        self.calls.append({"model": model, "user": user, "system": system,
                           "json_schema": json_schema})
        value = self.responses
        if isinstance(value, dict):
            value = value[model]
        if callable(value) and not isinstance(value, (str, ChatResult, Exception)):
            value = value(model, user, system)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, ChatResult):
            return value
        return ChatResult(value, cost_usd=0.0, latency_ms=1, provider_blocked=False)


class FakeEmbedder:
    def __init__(self, vectors: dict[str, list[float]] | None = None,
                 default: list[float] | None = None):
        self.vectors = vectors or {}
        self.default = default
        self.calls: list[list[str]] = []

    @classmethod
    def same_vector(cls) -> "FakeEmbedder":
        vec = [1.0] + [0.0] * (DIM - 1)
        return cls(default=vec)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            if t in self.vectors:
                out.append(self.vectors[t])
            elif self.default is not None:
                out.append(self.default)
            else:
                raise KeyError(f"FakeEmbedder has no vector for {t!r}")
        return out


def unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]
