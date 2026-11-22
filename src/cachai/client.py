"""Una llamada de chat contra un endpoint concreto, guardando lo que el auditor mide.

Un Target es "modelo @ proveedor": o un proveedor fijado en OpenRouter
(`provider.only` + `allow_fallbacks: false`) o una API directa compatible con OpenAI
(50x, el proveedor tramposo local, etc.). Nunca lanza: un error es un dato de
confiabilidad y vuelve dentro del Reply.
"""
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx


OPENROUTER = "https://openrouter.ai/api/v1"
_MODEL_MARKERS = ("no endpoints", "model not found", "not a valid model")


def _error_message(body: Any) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or "")
    return str(err or "")


def _kind(status: int, message: str) -> str:
    """model: el modelo o el endpoint no existe; auth: clave inválida o sin crédito."""
    m = message.lower()
    if status == 404 or any(x in m for x in _MODEL_MARKERS):
        return "model"
    if status in (401, 402, 403):
        return "auth"
    return "other"


@dataclass(frozen=True)
class Target:
    name: str
    model: str
    base_url: str = OPENROUTER
    api_key: str = field(default="", repr=False)
    provider_tag: str | None = None  # fija el proveedor en OpenRouter
    quantization: str | None = None  # declarada por el proveedor (E1)
    price_out: float | None = None  # USD por millón de tokens de salida, declarado
    first_party: bool = False

    @property
    def via_openrouter(self) -> bool:
        return self.provider_tag is not None


@dataclass
class Reply:
    target: str
    probe: str
    text: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    served_by: str | None = None
    served_model: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    finish: str | None = None
    error: str | None = None
    error_kind: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        return asdict(self)


def auth_headers(api_key: str) -> dict:
    # sin clave no se manda el header: "Bearer " con espacio final es inválido
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def ask(target: Target, probe_id: str, messages: list[dict], *, http: httpx.Client,
        tools: list[dict] | None = None, max_tokens: int = 256,
        stream: bool = False) -> Reply:
    body: dict[str, Any] = {"model": target.model, "messages": messages,
                            "max_tokens": max_tokens, "temperature": 0, "seed": 7}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    if target.via_openrouter:
        body["provider"] = {"only": [target.provider_tag], "allow_fallbacks": False}
        body["usage"] = {"include": True}
        body["reasoning"] = {"exclude": True}
    reply = Reply(target=target.name, probe=probe_id)
    start = time.perf_counter()
    try:
        resp = http.post(f"{target.base_url.rstrip('/')}/chat/completions", json=body,
                         headers=auth_headers(target.api_key))
    except httpx.HTTPError as e:
        reply.error, reply.error_kind = f"red: {type(e).__name__}", "other"
        return reply
    reply.latency_ms = int((time.perf_counter() - start) * 1000)
    if stream:
        return _parse_stream(resp, reply)
    try:
        data = resp.json()
    except ValueError:
        data = None
    err = data.get("error") if isinstance(data, dict) else None
    if resp.status_code != 200 or err or not isinstance(data, dict):
        message = _error_message(data) or f"HTTP {resp.status_code}"
        reply.error = f"{resp.status_code}: {message}"[:300]
        reply.error_kind = _kind(resp.status_code, message)
        return reply
    choices = data.get("choices") or []
    if not choices:
        reply.error, reply.error_kind = "sin choices", "other"
        return reply
    msg = choices[0].get("message") or {}
    usage = data.get("usage") or {}
    reply.text = msg.get("content") or ""
    reply.finish = choices[0].get("finish_reason")
    reply.tool_calls = [_tool_call(tc) for tc in (msg.get("tool_calls") or [])]
    reply.prompt_tokens = usage.get("prompt_tokens")
    reply.completion_tokens = usage.get("completion_tokens")
    reply.reasoning_tokens = int(
        (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
    reply.cost_usd = float(usage.get("cost") or 0.0)
    reply.served_by = data.get("provider")
    reply.served_model = data.get("model")
    return reply


def _parse_stream(resp: httpx.Response, reply: Reply) -> Reply:
    if resp.status_code != 200:
        try:
            data = resp.json()
        except ValueError:
            data = None
        message = _error_message(data) or f"HTTP {resp.status_code}"
        reply.error = f"{resp.status_code}: {message}"[:300]
        reply.error_kind = _kind(resp.status_code, message)
        return reply
    text_parts: list[str] = []
    tc_by_idx: dict[int, dict[str, str]] = {}
    saw_chunk = False
    saw_choice = False
    for line in resp.text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("error"):
            message = _error_message(chunk) or "stream error"
            reply.error = f"200: {message}"[:300]
            reply.error_kind = _kind(200, message)
            return reply
        saw_chunk = True
        if chunk.get("provider"):
            reply.served_by = chunk["provider"]
        if chunk.get("model"):
            reply.served_model = chunk["model"]
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            if usage.get("prompt_tokens") is not None:
                reply.prompt_tokens = usage["prompt_tokens"]
            if usage.get("completion_tokens") is not None:
                reply.completion_tokens = usage["completion_tokens"]
            rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            if rt is not None:
                reply.reasoning_tokens = int(rt)
            if usage.get("cost") is not None:
                reply.cost_usd = float(usage["cost"])
        choices = chunk.get("choices") or []
        if not choices:
            continue
        saw_choice = True
        c0 = choices[0]
        if c0.get("finish_reason"):
            reply.finish = c0["finish_reason"]
        delta = c0.get("delta") or {}
        if delta.get("content"):
            text_parts.append(delta["content"])
        for pos, tc in enumerate(delta.get("tool_calls") or []):
            idx = pos if tc.get("index") is None else int(tc["index"])
            acc = tc_by_idx.setdefault(idx, {"name": "", "arguments": ""})
            fn = tc.get("function") or {}
            if fn.get("name"):
                acc["name"] += fn["name"]
            if fn.get("arguments"):
                acc["arguments"] += fn["arguments"]
    if not saw_chunk:
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("error"):
            message = _error_message(data) or f"HTTP {resp.status_code}"
            reply.error = f"{resp.status_code}: {message}"[:300]
            reply.error_kind = _kind(resp.status_code, message)
            return reply
    if not saw_choice:
        reply.error, reply.error_kind = "sin choices", "other"
        return reply
    reply.text = "".join(text_parts)
    reply.tool_calls = [_tool_call({"function": tc_by_idx[i]}) for i in sorted(tc_by_idx)]
    return reply


def _tool_call(tc: dict) -> dict:
    fn = tc.get("function") or {}
    raw = fn.get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        args = None
    return {"name": fn.get("name"), "arguments": args, "raw": raw if args is None else None}
