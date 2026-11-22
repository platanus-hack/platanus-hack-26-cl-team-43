"""El auditor de punta a punta contra el modelo de juguete y el proveedor tramposo.

Sin red ni base de datos: los endpoints son apps ASGI servidas por TestClient.
"""
import random

import httpx
import pytest
from fastapi.testclient import TestClient

from cachai import checks, endpoints, ficha, scan, suite
from cachai.client import Target, ask
from cachai.toy import create_toy
from cachai.villain import HIDDEN_PROMPT, create_villain


class Router(httpx.Client):
    """Un cliente que reparte por host a varias apps locales."""

    def __init__(self, apps: dict):
        super().__init__()
        self.clients = {host: TestClient(app, base_url=f"http://{host}")
                        for host, app in apps.items()}

    def post(self, url, **kw):
        return self.clients[httpx.URL(url).host].post(url, **kw)


def _world(**villain_kw):
    toy = create_toy()
    upstream = TestClient(toy, base_url="http://toy")
    villain = create_villain("http://toy/v1", "", http=upstream,
                             rng=random.Random(1), **villain_kw)
    http = Router({"toy": toy, "villain": villain})
    targets = [Target("oficial", "toy-large", base_url="http://toy/v1"),
               Target("honesto", "toy-large", base_url="http://toy/v1"),
               Target("tramposo", "toy-large", base_url="http://villain/v1")]
    return http, targets, villain


def _audit(seed, **villain_kw):
    http, targets, villain = _world(**villain_kw)
    probes = suite.build(seed)
    replies, _ = scan.run(targets, probes, http=http)
    cards = {c.target: c for c in checks.score(probes, replies, "oficial")}
    return cards, replies, targets, villain


def test_honest_endpoint_is_never_flagged():
    cards, *_ = _audit(7, inject=HIDDEN_PROMPT)
    assert cards["honesto"].flags == []
    assert cards["honesto"].token_verdict == "sin excedente"
    assert cards["honesto"].tasks_ok == cards["honesto"].tasks_n == 20


def test_hidden_prompt_shows_as_constant_token_excess():
    cards, *_ = _audit(7, inject=HIDDEN_PROMPT)
    c = cards["tramposo"]
    expected = 3 + len(HIDDEN_PROMPT.split())  # un mensaje más en el juguete
    assert c.hidden_tokens == expected
    assert set(c.token_deltas) == {expected}
    assert c.canary_leaks == c.canary_n  # el prompt oculto rompe el secreto del cliente
    assert c.distinguishable


def test_model_swap_is_caught_by_accuracy():
    cards, *_ = _audit(7, swap_model="toy-small")
    c = cards["tramposo"]
    assert c.token_verdict == "sin excedente"
    assert c.tasks_ok < c.tasks_n and c.tasks_p < checks.ALPHA
    assert c.served_models == ["toy-large"]  # el tramposo declara el modelo pedido
    assert c.distinguishable


def test_evading_villain_passes_public_suite_and_fails_blind_one():
    public, *_ = _audit(None, inject=HIDDEN_PROMPT, swap_model="toy-small", evade=True)
    assert public["tramposo"].flags == []
    blind, _, _, villain = _audit(7, inject=HIDDEN_PROMPT, swap_model="toy-small", evade=True)
    assert blind["tramposo"].distinguishable
    assert villain.state.stats["honest"] == 0


def test_partial_swap_needs_pooled_evidence():
    cards, *_ = _audit(7, swap_model="toy-small", fraction=0.4)
    c = cards["tramposo"]
    assert c.pooled_p is not None and c.failed > 0
    assert cards["honesto"].pooled_p == 1.0


def test_blind_suite_is_reproducible_and_differs_from_public():
    assert suite.build(5) == suite.build(5)
    assert suite.build(5) != suite.build(6)
    blind = {suite.fingerprint(p.as_messages()) for p in suite.build(5)}
    assert not blind & suite.public_fingerprints()


def test_consensus_reference_flags_the_odd_one_out():
    http, targets, _ = _world(inject=HIDDEN_PROMPT)
    targets.append(Target("otro", "toy-large", base_url="http://toy/v1"))
    probes = suite.build(3)
    replies, _ = scan.run(targets, probes, http=http)
    cards = {c.target: c for c in checks.score(probes, replies, checks.CONSENSUS)}
    assert cards["tramposo"].hidden_tokens and not cards["otro"].flags


def test_unreachable_endpoint_is_reported_not_raised():
    http, targets, _ = _world(inject=HIDDEN_PROMPT)
    r = ask(Target("x", "no-such-model", base_url="http://toy/v1"), "p",
            [{"role": "user", "content": "hi"}], http=http)
    assert not r.ok and r.error_kind == "model"


def test_openrouter_target_pins_the_provider():
    seen = {}

    def handler(request):
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "a/b", "provider": "Wafer",
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 1, "cost": 0.001}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    r = ask(Target("wafer/us", "a/b", api_key="k", provider_tag="wafer/us"), "p",
            [{"role": "user", "content": "hi"}], http=http)
    assert seen["provider"] == {"only": ["wafer/us"], "allow_fallbacks": False}
    assert seen["temperature"] == 0
    assert (r.prompt_tokens, r.served_by, r.cost_usd) == (9, "Wafer", 0.001)


def test_spend_cap_stops_the_scan():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "1"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.5}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    targets = [Target("a", "m", api_key="k", provider_tag="a")]
    replies, spend = scan.run(targets, suite.build(1), http=http, max_usd=1.0)
    assert len(replies["a"]) == 2 and spend.spent == 1.0


def test_auth_failure_stops_that_endpoint():
    http = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, json={"error": {"message": "bad key"}})))
    replies, _ = scan.run([Target("a", "m", api_key="k", provider_tag="a")],
                          suite.build(1), http=http)
    assert len(replies["a"]) == 1


def test_fisher_matches_known_values():
    assert checks.fisher(0, 8, 8, 8) == pytest.approx(2 / 12870)
    assert checks.fisher(5, 10, 5, 10) == pytest.approx(1.0)
    assert checks.fisher(0, 0, 3, 4) == 1.0


def test_normalize_handles_how_models_write_numbers():
    assert checks.normalize("The answer is **12,345**.", "12345") == "12345"
    assert checks.normalize("12, 40, 7", "7,12,40") == "12,40,7"
    assert checks.normalize('"Amber  Kelp"', "amber kelp") == "amber kelp"


ENDPOINTS = [
    {"tag": "z-ai/fp8", "quantization": "fp8", "pricing": {"completion": "0.000004"}},
    {"tag": "cheap/us", "quantization": "unknown", "pricing": {"completion": "0.000001"}},
    {"tag": "mid/fp4", "quantization": "fp4", "pricing": {"completion": "0.000002"}},
]


def test_declared_summary_and_pick():
    d = endpoints.declared_summary(ENDPOINTS)
    assert (d["endpoints"], d["sin_cuantizacion_declarada"], d["precio_ratio"]) == (3, 1, 4.0)
    targets = endpoints.to_targets("z-ai/glm", ENDPOINTS, "k")
    picked = endpoints.pick(targets, 2)
    assert [t.name for t in picked] == ["z-ai/fp8", "cheap/us"]
    assert picked[0].first_party and picked[1].quantization is None
    with pytest.raises(ValueError):
        endpoints.pick(targets, 2, ["nope"])


def test_ficha_separates_evidence_levels_and_hash_is_stable():
    cards, replies, targets, _ = _audit(7, inject=HIDDEN_PROMPT)
    f = ficha.build("toy-large", targets, list(cards.values()), replies,
                    reference="oficial", suite="ciego", seed=7, declared=None, spent_usd=0)
    md = ficha.to_markdown(f)
    assert "DISTINGUIBLE" in md and "sin desviación detectada" in md
    assert "cachai scan toy-large --seed 7" in md
    assert "No se certifica privacidad" in md
    for r in replies["tramposo"].values():
        r.latency_ms = 0
    again = ficha.evidence_hash(replies)
    assert len(f["evidencia_sha256"]) == 64 and again != ""


def test_ficha_slug_is_a_lowercase_url_safe_name():
    cards, replies, targets, _ = _audit(7, inject=HIDDEN_PROMPT)
    f = ficha.build("z-ai/glm-5.3", targets, list(cards.values()), replies,
                    reference="oficial", suite="ciego", seed=7, declared=None, spent_usd=0)
    slug = ficha.slug(f)
    assert slug.startswith("ficha-z-ai-glm-5-3-") and slug == slug.lower()


def test_rate_limit_is_retried_not_counted_as_failure(monkeypatch):
    """Un 429 habla del límite de la cuenta, no del modelo: la prueba se repite."""
    probes = suite.build(5, n_tasks=4)
    calls, waits = [], []

    def fake_ask(target, probe_id, messages, *, http, tools=None, max_tokens=256, stream=False):
        calls.append((probe_id, max_tokens))
        first = sum(pid == probe_id for pid, _ in calls) == 1
        if first and probe_id == probes[0].id:
            return scan.Reply(target=target.name, probe=probe_id,
                              error="429: Rate limit exceeded", error_kind="other")
        return scan.Reply(target=target.name, probe=probe_id, text="ok")

    monkeypatch.setattr(scan, "ask", fake_ask)
    out = scan._run_target(Target("ref", "m"), probes, None, scan.Spend(1.0), None,
                           extra_tokens=512, sleep=waits.append)
    assert all(r.ok for r in out.values()) and len(out) == len(probes)
    assert waits == [scan.RATE_LIMIT_WAIT_S]
    # el margen de tokens no se aplica a las pruebas de conteo
    asked = dict(calls)
    for p in probes:
        assert asked[p.id] == p.max_tokens + (0 if p.kind == "tokens" else 512)
