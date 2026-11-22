"""Modelo de juguete, compatible con la API de OpenAI, para probar el auditor sin claves.

No es un LLM: responde con reglas fijas. `toy-large` hace bien las tareas; `toy-small`
falla multiplicaciones y ordenamientos, rompe algunas tool calls y filtra el secreto del
system prompt. Sirve para tests y para ensayar la demo; lo que se mida contra él no es
un hallazgo sobre ningún proveedor.
"""
import json
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MODELS = ("toy-large", "toy-small")
_PER_MESSAGE = 3  # sobrecosto fijo de plantilla por mensaje


def count_tokens(messages: list[dict]) -> int:
    return sum(_PER_MESSAGE + len(str(m.get("content") or "").split()) for m in messages)


def _answer(model: str, messages: list[dict], tools) -> tuple[str, list[dict]]:
    small = model == "toy-small"
    system = " ".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    user = str(messages[-1].get("content") or "")
    if tools and (m := re.search(r"weather in (.+?) right now", user)):
        city = m.group(1)
        unit = "C" if small and len(city) % 2 else "celsius"
        return "", [{"id": "call_1", "type": "function", "function": {
            "name": "get_weather", "arguments": json.dumps({"city": city, "unit": unit})}}]
    if (m := re.search(r"Secret token: (\S+?)\.", system)) and "secret token" in user.lower():
        told_to_reveal = "reveal it" in system.lower()
        return (m.group(1) if small or told_to_reveal else "I can't share that."), []
    if m := re.search(r"What is (\d+) \+ (\d+)", user):
        return str(int(m.group(1)) + int(m.group(2))), []
    if m := re.search(r"What is (\d+) \* (\d+)", user):
        return str(int(m.group(1)) * int(m.group(2)) + (1 if small else 0)), []
    if m := re.search(r"ascending order: ([\d, ]+)\.", user):
        nums = [int(x) for x in re.findall(r"\d+", m.group(1))]
        ordered = nums if small else sorted(nums)
        return ", ".join(map(str, ordered)), []
    if m := re.search(r"Repeat exactly, with nothing else: (.+)", user):
        return m.group(1), []
    return "ok", []


def create_toy() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        model = body.get("model")
        if model not in MODELS:
            return JSONResponse({"error": {"message": "model not found", "code": 404}}, 404)
        messages = body.get("messages") or []
        text, calls = _answer(model, messages, body.get("tools"))
        message = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = calls
        return {"model": model, "choices": [{
            "index": 0, "message": message,
            "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": count_tokens(messages),
                      "completion_tokens": max(1, len(text.split())), "cost": 0.0}}

    return app
