import time
from collections.abc import Callable
from typing import Any

import httpx

from radar.llm.base import ChatResult, LLMError

URL = "https://openrouter.ai/api/v1/chat/completions"


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _error_message(body: Any) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or "")
    return str(err or "")


def _is_moderation(message: str) -> bool:
    m = message.lower()
    return "moderation" in m or "flagged" in m


class OpenRouterClient:
    def __init__(self, api_key: str, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.perf_counter):
        self.api_key = api_key
        self.http = http or httpx.Client(timeout=120)
        self.clock = clock

    def chat(self, model: str, user: str, *, system: str | None = None,
             json_schema: dict | None = None, max_tokens: int = 600) -> ChatResult:
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": user})
        body: dict[str, Any] = {"model": model, "messages": messages,
                                "max_tokens": max_tokens, "usage": {"include": True}}
        if json_schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "out", "strict": True, "schema": json_schema}}
        start = self.clock()
        try:
            resp = self.http.post(URL, json=body,
                                  headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as e:
            raise LLMError(f"openrouter network error: {e!r}", retryable=True) from e
        latency_ms = int((self.clock() - start) * 1000)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code != 200:
            return self._failure(resp.status_code, data, latency_ms)
        if not isinstance(data, dict):
            raise LLMError("openrouter returned non-JSON body", retryable=True)
        if isinstance(data.get("error"), dict):
            code = data["error"].get("code")
            status = code if isinstance(code, int) else 502
            return self._failure(status, data, latency_ms)
        choice = (data.get("choices") or [{}])[0]
        blocked = choice.get("finish_reason") == "content_filter"
        text = (choice.get("message") or {}).get("content") or ""
        cost = (data.get("usage") or {}).get("cost") or 0.0
        return ChatResult(text, float(cost), latency_ms, blocked)

    @staticmethod
    def _failure(status: int, data: Any, latency_ms: int) -> ChatResult:
        message = _error_message(data)
        if status == 403 and _is_moderation(message):
            return ChatResult("", 0.0, latency_ms, True)
        raise LLMError(f"openrouter {status}: {message}", retryable=_retryable(status))
