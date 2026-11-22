"""TDD tests for KVV-inspired verifier improvements (Units 1, 2, 3)."""
import pytest

from cachai import checks, suite
from cachai.client import Reply


# --- Work Unit 1: Reasoning-Starvation Blind Spot ---


def test_suite_max_tokens_prevents_reasoning_starvation():
    probes = suite.build(7)
    free_probes = [p for p in probes if p.kind == "free"]
    canary_probes = [p for p in probes if p.kind == "canary"]
    assert free_probes and all(p.max_tokens >= 256 for p in free_probes)
    assert canary_probes and all(p.max_tokens >= 128 for p in canary_probes)


def test_empty_starved_replies_do_not_count_as_agreement():
    probes = [
        suite.Probe(f"blind-free-{i:02d}", "free", ({"role": "user", "content": f"q{i}"},), max_tokens=256)
        for i in range(6)
    ]
    starved = lambda target, pid: Reply(
        target=target, probe=pid, text="", finish="length", reasoning_tokens=48, completion_tokens=48
    )
    replies = {
        "ref": {p.id: starved("ref", p.id) for p in probes},
        "floor": {p.id: starved("floor", p.id) for p in probes},
        "cand": {p.id: starved("cand", p.id) for p in probes},
    }
    cards = {c.target: c for c in checks.score(probes, replies, "ref", floor="floor")}
    assert cards["cand"].free_n == 0
    assert cards["cand"].free_agree == 0
    assert cards["floor"].free_n == 0
    assert cards["cand"].agree_p is None
    assert not cards["cand"].distinguishable


def test_empty_candidate_against_nonempty_reference_counts_as_disagreement():
    probes = [
        suite.Probe(f"blind-free-{i:02d}", "free", ({"role": "user", "content": f"q{i}"},), max_tokens=256)
        for i in range(10)
    ]
    replies = {
        "ref": {p.id: Reply("ref", p.id, text=f"A calm lighthouse {i}.", finish="stop") for i, p in enumerate(probes)},
        "floor": {p.id: Reply("floor", p.id, text=f"A calm lighthouse {i}.", finish="stop") for i, p in enumerate(probes)},
        "cand": {p.id: Reply("cand", p.id, text="", finish="length", reasoning_tokens=48) for p in probes},
    }
    cards = {c.target: c for c in checks.score(probes, replies, "ref", floor="floor")}
    assert cards["cand"].free_n == 10
    assert cards["cand"].free_agree == 0
    assert cards["cand"].agree_p is not None and cards["cand"].agree_p < checks.ALPHA
    assert cards["cand"].distinguishable


def test_empty_task_replies_do_not_inflate_task_agreement():
    probes = [
        suite.Probe("blind-task-00", "task", ({"role": "user", "content": "What is 1 + 1?"},), expected="2"),
    ]
    replies = {
        "ref": {"blind-task-00": Reply("ref", "blind-task-00", text="", finish="length", reasoning_tokens=48)},
        "cand": {"blind-task-00": Reply("cand", "blind-task-00", text="", finish="length", reasoning_tokens=48)},
    }
    cards = {c.target: c for c in checks.score(probes, replies, "ref")}
    assert cards["cand"].agree_n == 0
    assert cards["cand"].agree == 0


# --- Work Unit 2: KVV-Inspired A4 Tool-Call Verifier Upgrade ---


def test_client_ask_streaming_reassembles_text_and_tool_calls_and_usage():
    import json
    import httpx
    from cachai.client import Target, ask

    seen = {}
    chunks = [
        {"model": "m-served", "provider": "50x", "choices": [{"index": 0, "delta": {"content": "Hel"}}]},
        {"model": "m-served", "choices": [{"index": 0, "delta": {"content": "lo"}}]},
        {"model": "m-served", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "c1", "type": "function", "function": {"name": "get_wea", "arguments": '{"city": "Li'}}
        ]}}]},
        {"model": "m-served", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"name": "ther", "arguments": 'ma", "unit": "celsius"}'}}
        ]}, "finish_reason": "tool_calls"}]},
        {"model": "m-served", "choices": [], "usage": {
            "prompt_tokens": 12, "completion_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 5}, "cost": 0.002,
        }},
    ]
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    r = ask(Target("t", "m", base_url="http://x/v1"), "p1", [{"role": "user", "content": "hi"}],
            http=http, tools=[suite.WEATHER_TOOL], stream=True)
    assert seen["stream"] is True
    assert seen["stream_options"] == {"include_usage": True}
    assert r.ok
    assert r.text == "Hello"
    assert r.finish == "tool_calls"
    assert r.prompt_tokens == 12
    assert r.completion_tokens == 20
    assert r.reasoning_tokens == 5
    assert r.cost_usd == 0.002
    assert r.served_by == "50x"
    assert r.served_model == "m-served"
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0]["name"] == "get_weather"
    assert r.tool_calls[0]["arguments"] == {"city": "Lima", "unit": "celsius"}


def test_tool_valid_rejects_multi_dict_concat():
    p = suite.Probe("t0", "tool", ({"role": "user", "content": "weather in Lima"},), expected="Lima")
    r = Reply("cand", "t0", tool_calls=[{
        "name": "get_weather",
        "arguments": None,
        "raw": '{"city": "Lima", "unit": "celsius"}{"city": "Lima", "unit": "celsius"}',
    }])
    assert checks._is_multi_dict_concat(r.tool_calls[0]["raw"])
    assert not checks.tool_valid(p, r)


@pytest.mark.parametrize("leaked", [
    "<|tool_call_end|>",
    "<|open|>",
    "<|sep|>",
    "<|close|>",
    "<|end_of_msg|>",
    "<｜tool▁call▁end｜>",
])
def test_tool_valid_rejects_leaked_special_tokens(leaked):
    p = suite.Probe("t0", "tool", ({"role": "user", "content": "weather in Lima"},), expected="Lima")
    # Leaked in reply.text
    r_text = Reply("cand", "t0", text=f"calling {leaked}", tool_calls=[{
        "name": "get_weather", "arguments": {"city": "Lima", "unit": "celsius"}, "raw": None,
    }])
    assert not checks.tool_valid(p, r_text)
    # Leaked in tool arguments value
    r_arg = Reply("cand", "t0", tool_calls=[{
        "name": "get_weather", "arguments": {"city": f"Lima{leaked}", "unit": "celsius"}, "raw": None,
    }])
    assert not checks.tool_valid(p, r_arg)


def test_suite_includes_nested_defs_and_streaming_tool_probes():
    probes = suite.build(7)
    tool_probes = [p for p in probes if p.kind == "tool"]
    assert len(tool_probes) >= 8
    kinds = {p.schema_kind for p in tool_probes}
    assert {"weather", "order", "alert"}.issubset(kinds)
    assert any(p.stream for p in tool_probes)
    assert any(not p.stream for p in tool_probes)
    # Check alert probe has $defs/$ref and multi-tool distractor
    alert_probe = next(p for p in tool_probes if p.schema_kind == "alert")
    assert len(alert_probe.tools) >= 2
    alert_params = alert_probe.tools[0]["function"]["parameters"]
    assert "$defs" in alert_params


def test_tool_valid_validates_order_and_alert_schemas_strictly():
    probes = suite.build(7)
    order_p = next(p for p in probes if p.kind == "tool" and p.schema_kind == "order")
    alert_p = next(p for p in probes if p.kind == "tool" and p.schema_kind == "alert")

    import json
    exp_o = json.loads(order_p.expected)
    good_order = Reply("c", order_p.id, tool_calls=[{
        "name": "create_order",
        "arguments": {
            "order_id": exp_o["order_id"],
            "priority": exp_o["priority"],
            "items": [{"sku": exp_o["sku"], "qty": exp_o["qty"]}],
        },
        "raw": None,
    }])
    assert checks.tool_valid(order_p, good_order)

    # Extra property violates additionalProperties: false
    bad_order = Reply("c", order_p.id, tool_calls=[{
        "name": "create_order",
        "arguments": {
            "order_id": exp_o["order_id"],
            "priority": exp_o["priority"],
            "items": [{"sku": exp_o["sku"], "qty": exp_o["qty"], "extra": 1}],
        },
        "raw": None,
    }])
    assert not checks.tool_valid(order_p, bad_order)

    exp_a = json.loads(alert_p.expected)
    good_alert = Reply("c", alert_p.id, tool_calls=[{
        "name": "emit_alert",
        "arguments": {
            "severity": exp_a["severity"],
            "target": {"service": exp_a["service"], "region": exp_a["region"]},
        },
        "raw": None,
    }])
    assert checks.tool_valid(alert_p, good_alert)

    # Wrong tool (distractor ping_host)
    bad_alert = Reply("c", alert_p.id, tool_calls=[{
        "name": "ping_host",
        "arguments": {"host": exp_a["service"]},
        "raw": None,
    }])
    assert not checks.tool_valid(alert_p, bad_alert)


def test_standalone_tools_p_flags_broken_tool_endpoint():
    probes = [p for p in suite.build(7) if p.kind == "tool"]
    import json
    good_replies = {}
    bad_replies = {}
    for p in probes:
        if p.schema_kind == "order":
            e = json.loads(p.expected)
            tc = {"name": "create_order", "arguments": {
                "order_id": e["order_id"], "priority": e["priority"],
                "items": [{"sku": e["sku"], "qty": e["qty"]}]}, "raw": None}
        elif p.schema_kind == "alert":
            e = json.loads(p.expected)
            tc = {"name": "emit_alert", "arguments": {
                "severity": e["severity"],
                "target": {"service": e["service"], "region": e["region"]}}, "raw": None}
        else:
            tc = {"name": "get_weather", "arguments": {"city": p.expected, "unit": "celsius"}, "raw": None}
        good_replies[p.id] = Reply("ref", p.id, tool_calls=[tc])
        bad_replies[p.id] = Reply("cand", p.id, tool_calls=[{
            "name": tc["name"], "arguments": None, "raw": '{"a":1}{"b":2}',
        }])

    cards = {c.target: c for c in checks.score(probes, {"ref": good_replies, "cand": bad_replies}, "ref")}
    cand = cards["cand"]
    assert cand.tools_ok == 0 and cand.tools_n == len(probes)
    assert cand.tools_p is not None and cand.tools_p < checks.ALPHA
    assert any("tool calls" in f.lower() for f in cand.flags)
    assert cand.distinguishable


def test_toy_end_to_end_handles_new_tool_schemas_and_streaming():
    from fastapi.testclient import TestClient
    from cachai import scan
    from cachai.client import Target
    from cachai.toy import create_toy

    http = TestClient(create_toy(), base_url="http://toy")
    probes = [p for p in suite.build(7) if p.kind == "tool"]
    targets = [
        Target("ref", "toy-large", base_url="http://toy/v1"),
        Target("small", "toy-small", base_url="http://toy/v1"),
    ]
    replies, _ = scan.run(targets, probes, http=http)
    cards = {c.target: c for c in checks.score(probes, replies, "ref")}
    assert cards["ref"].tools_ok == len(probes)
    assert cards["ref"].errors == 0
    assert cards["small"].tools_ok < len(probes)
    assert cards["small"].tools_p is not None and cards["small"].tools_p < checks.ALPHA
    assert cards["small"].distinguishable


# --- Work Unit 3: 50x Preset & Calibrated R2 Prompt-Token Tolerance ---


def test_50x_preset_resolution(monkeypatch):
    from cachai import cli, providers

    assert "50x" in providers.PRESETS
    p = providers.PRESETS["50x"]
    assert p.base_url == "https://api.ai.50x.energy/v1"
    assert p.key_env == "50X_API_KEY"
    assert p.keys_url == "https://ai.50x.energy/docs"

    monkeypatch.setenv("50X_API_KEY", "test-50x-key")
    t = cli._endpoint("50x:deepseek-ai/DeepSeek-V4.1-Flash-1017")
    assert t.base_url == "https://api.ai.50x.energy/v1"
    assert t.api_key == "test-50x-key"
    assert t.model == "deepseek-ai/DeepSeek-V4.1-Flash-1017"


@pytest.mark.parametrize("delta", [1, 2, 3, -1, -2, -3])
def test_constant_token_delta_within_template_tolerance_does_not_flag(delta):
    assert checks.TOKEN_TEMPLATE_TOLERANCE == 3
    probes = [
        suite.Probe(f"blind-tokens-{i}", "tokens", ({"role": "user", "content": f"w{i}"},), max_tokens=8)
        for i in range(6)
    ]
    replies = {
        "ref": {p.id: Reply("ref", p.id, text="ok", prompt_tokens=10 + i) for i, p in enumerate(probes)},
        "cand": {p.id: Reply("cand", p.id, text="ok", prompt_tokens=10 + i + delta) for i, p in enumerate(probes)},
    }
    cards = {c.target: c for c in checks.score(probes, replies, "ref")}
    cand = cards["cand"]
    assert cand.token_deltas == [delta] * 6
    assert cand.hidden_tokens is None
    assert cand.flags == []
    assert not cand.distinguishable


@pytest.mark.parametrize("delta", [4, 10, -4])
def test_constant_token_delta_above_template_tolerance_flags(delta):
    probes = [
        suite.Probe(f"blind-tokens-{i}", "tokens", ({"role": "user", "content": f"w{i}"},), max_tokens=8)
        for i in range(6)
    ]
    replies = {
        "ref": {p.id: Reply("ref", p.id, text="ok", prompt_tokens=10 + i) for i, p in enumerate(probes)},
        "cand": {p.id: Reply("cand", p.id, text="ok", prompt_tokens=10 + i + delta) for i, p in enumerate(probes)},
    }
    cards = {c.target: c for c in checks.score(probes, replies, "ref")}
    cand = cards["cand"]
    assert cand.hidden_tokens == delta
    assert cand.distinguishable
    assert any("tokens de prompt" in f for f in cand.flags)


def test_variable_token_deltas_still_flag():
    probes = [
        suite.Probe(f"blind-tokens-{i}", "tokens", ({"role": "user", "content": f"w{i}"},), max_tokens=8)
        for i in range(6)
    ]
    deltas = [0, 1, 2, 0, 3, 1]
    replies = {
        "ref": {p.id: Reply("ref", p.id, text="ok", prompt_tokens=10 + i) for i, p in enumerate(probes)},
        "cand": {p.id: Reply("cand", p.id, text="ok", prompt_tokens=10 + i + deltas[i]) for i, p in enumerate(probes)},
    }
    cards = {c.target: c for c in checks.score(probes, replies, "ref")}
    cand = cards["cand"]
    assert cand.distinguishable
    assert any("conteo variable" in f for f in cand.flags)


# --- Code Review Fixes (TDD) ---


def test_stream_http_200_json_error_body_is_reported():
    import httpx
    from cachai.client import Target, ask

    http = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"error": {"message": "no endpoints found"}})
    ))
    r = ask(Target("t", "m", base_url="http://x/v1"), "p1", [{"role": "user", "content": "hi"}],
            http=http, stream=True)
    assert not r.ok
    assert r.error_kind == "model"
    assert "no endpoints found" in (r.error or "")


def test_stream_usage_only_without_choices_reports_sin_choices():
    import json
    import httpx
    from cachai.client import Target, ask

    sse = f"data: {json.dumps({'model': 'm', 'choices': [], 'usage': {'prompt_tokens': 5}})}\n\ndata: [DONE]\n\n"
    http = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    ))
    r = ask(Target("t", "m", base_url="http://x/v1"), "p1", [{"role": "user", "content": "hi"}],
            http=http, stream=True)
    assert not r.ok
    assert r.error == "sin choices"


def test_stream_tool_calls_without_index_do_not_collide():
    import json
    import httpx
    from cachai.client import Target, ask

    chunk = {"model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "a", "arguments": '{"x": 1}'}},
        {"id": "c2", "type": "function", "function": {"name": "b", "arguments": '{"y": 2}'}},
    ]}, "finish_reason": "tool_calls"}]}
    sse = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    http = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    ))
    r = ask(Target("t", "m", base_url="http://x/v1"), "p1", [{"role": "user", "content": "hi"}],
            http=http, stream=True)
    assert r.ok
    assert len(r.tool_calls) == 2
    assert r.tool_calls[0]["name"] == "a" and r.tool_calls[0]["arguments"] == {"x": 1}
    assert r.tool_calls[1]["name"] == "b" and r.tool_calls[1]["arguments"] == {"y": 2}


def test_tools_p_is_two_sided_like_tasks_p_and_canary_p():
    probes = [
        suite.Probe(f"blind-tool-{i}", "tool", ({"role": "user", "content": "weather in Lima"},), expected="Lima")
        for i in range(8)
    ]
    bad = lambda t, pid: Reply(t, pid, tool_calls=[{"name": "get_weather", "arguments": None, "raw": "{}{}"}])
    good = lambda t, pid: Reply(t, pid, tool_calls=[{
        "name": "get_weather", "arguments": {"city": "Lima", "unit": "celsius"}, "raw": None,
    }])
    replies = {
        "ref": {p.id: bad("ref", p.id) for p in probes},
        "cand": {p.id: good("cand", p.id) for p in probes},
    }
    cards = {c.target: c for c in checks.score(probes, replies, "ref")}
    cand = cards["cand"]
    assert cand.tools_p is not None and cand.tools_p < checks.ALPHA
    assert cand.distinguishable
    assert any("tool calls" in f.lower() for f in cand.flags)
