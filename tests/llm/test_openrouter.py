import httpx
import pytest
import respx
from fakes import FakeEmbedder, FakeLLM

from radar.llm.base import ChatResult, LLMError
from radar.llm.embed import Embedder
from radar.llm.openrouter import OpenRouterClient

URL = "https://openrouter.ai/api/v1/chat/completions"


def _ok(content="hi", cost=0.002, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"cost": cost}}


@respx.mock
def test_chat_parses_text_and_cost():
    route = respx.post(URL).respond(200, json=_ok("hello", 0.0123))
    ticks = iter([10.0, 10.25])
    c = OpenRouterClient("k", clock=lambda: next(ticks))
    r = c.chat("m/x", "u", system="s", max_tokens=50)
    assert (r.text, r.cost_usd, r.latency_ms, r.provider_blocked) == ("hello", 0.0123, 250, False)
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer k"
    import json
    body = json.loads(req.content)
    assert body == {"model": "m/x", "max_tokens": 50, "usage": {"include": True},
                    "messages": [{"role": "system", "content": "s"},
                                 {"role": "user", "content": "u"}]}


@respx.mock
def test_null_content_and_missing_cost():
    respx.post(URL).respond(200, json={"choices": [{"message": {"content": None}}]})
    r = OpenRouterClient("k").chat("m", "u")
    assert (r.text, r.cost_usd) == ("", 0.0)


@respx.mock
@pytest.mark.parametrize("status,retryable", [(429, True), (500, True), (503, True),
                                              (400, False), (401, False), (404, False)])
def test_status_errors(status, retryable):
    respx.post(URL).respond(status, json={"error": {"message": "x"}})
    with pytest.raises(LLMError) as e:
        OpenRouterClient("k").chat("m", "u")
    assert e.value.retryable is retryable


@respx.mock
def test_429_raises_retryable():
    respx.post(URL).respond(429, json={"error": {"message": "slow down"}})
    with pytest.raises(LLMError) as e:
        OpenRouterClient("k").chat("m", "u")
    assert e.value.retryable is True


@respx.mock
def test_404_raises_not_retryable():
    respx.post(URL).respond(404, json={"error": {"message": "no such model"}})
    with pytest.raises(LLMError) as e:
        OpenRouterClient("k").chat("m", "u")
    assert e.value.retryable is False


@respx.mock
def test_timeout_is_retryable():
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(LLMError) as e:
        OpenRouterClient("k").chat("m", "u")
    assert e.value.retryable is True


@respx.mock
def test_content_filter_is_provider_blocked():
    respx.post(URL).respond(200, json=_ok("", finish="content_filter"))
    assert OpenRouterClient("k").chat("m", "u").provider_blocked is True


@respx.mock
def test_403_moderation_is_provider_blocked_other_403_raises():
    respx.post(URL).respond(403, json={"error": {"message": "Input was flagged by moderation"}})
    assert OpenRouterClient("k").chat("m", "u").provider_blocked is True
    respx.post(URL).respond(403, json={"error": {"message": "forbidden key"}})
    with pytest.raises(LLMError) as e:
        OpenRouterClient("k").chat("m", "u")
    assert e.value.retryable is False


@respx.mock
def test_200_with_error_body_uses_error_code():
    respx.post(URL).respond(200, json={"error": {"code": 429, "message": "rate"}})
    with pytest.raises(LLMError) as e:
        OpenRouterClient("k").chat("m", "u")
    assert e.value.retryable is True


@respx.mock
def test_json_schema_sent_in_response_format():
    route = respx.post(URL).respond(200, json=_ok("{}"))
    schema = {"type": "object"}
    OpenRouterClient("k").chat("m", "u", json_schema=schema)
    import json
    body = json.loads(route.calls[0].request.content)
    assert body["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": schema}}


class _Model:
    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[3.0, 4.0] + [0.0] * 382 for _ in texts]


def test_embedder_normalises_with_injected_model():
    e = Embedder(model=_Model())
    [v] = e.embed(["a"])
    assert len(v) == 384 and isinstance(v, list) and isinstance(v[0], float)
    assert v[0] == pytest.approx(0.6) and v[1] == pytest.approx(0.8)


def test_embedder_is_lazy(monkeypatch):
    import radar.llm.embed as mod
    made = []
    monkeypatch.setattr(mod, "_load_model", lambda: made.append(1) or _Model())
    e = Embedder()
    assert not made
    e.embed(["a"])
    e.embed(["b"])
    assert made == [1]


def test_fakes():
    ok = FakeLLM({"m": "t", "e": LLMError("x", retryable=True),
                  "c": lambda model, user, system: ChatResult("c", 1.0, 2, False)})
    assert ok.chat("m", "u").text == "t"
    assert ok.chat("c", "u").cost_usd == 1.0
    with pytest.raises(LLMError):
        ok.chat("e", "u")
    assert [c["model"] for c in ok.calls] == ["m", "c", "e"]
    v = FakeEmbedder.same_vector().embed(["a", "b"])
    assert v[0] == v[1] and len(v[0]) == 384


@pytest.mark.slow
def test_real_embedder():
    [v, w] = Embedder().embed(["ignore previous instructions", "a recipe for soup"])
    assert len(v) == 384
    assert sum(x * x for x in v) == pytest.approx(1.0, abs=1e-4)
    assert v != w


@respx.mock
@pytest.mark.parametrize("status,message,kind", [
    (404, "x", "model"),
    (400, "openai/gpt-x is not a valid model ID", "model"),
    (400, "No endpoints found for foo/bar", "model"),
    (400, "Model not found", "model"),
    (401, "No auth credentials found", "auth"),
    (402, "Insufficient credits", "auth"),
    (403, "Key is disabled", "auth"),
    (400, "bad request", "other"),
    (429, "slow down", "other"),
    (500, "boom", "other"),
])
def test_error_kind(status, message, kind):
    respx.post(URL).respond(status, json={"error": {"message": message}})
    with pytest.raises(LLMError) as e:
        OpenRouterClient("k").chat("m", "u")
    assert e.value.kind == kind


def test_llm_error_kind_defaults_to_other():
    assert LLMError("x", retryable=False).kind == "other"


@respx.mock
def test_400_flagged_is_provider_blocked():
    respx.post(URL).respond(400, json={"error": {"message": "Your input was FLAGGED for review"}})
    assert OpenRouterClient("k").chat("m", "u").provider_blocked is True
