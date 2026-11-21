"""El atacante mueve segundo (#18): huellas del evaluador que una app podría reconocer."""

import http.server
import json
import random
import threading

from cachai import holdout
from cachai.holdout import Clients, exchange


class Echo:
    """Devuelve las cabeceras que recibió."""

    def __init__(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def _received(request: dict, rng=None) -> dict:
    import time
    app = Echo()
    try:
        with Clients(rng) as clients:
            hops, _ = exchange(clients.for_request(request), app.url, request, time.monotonic() + 10)
    finally:
        app.srv.shutdown()
    return json.loads(hops[-1][1])


def test_attacks_look_like_a_browser_not_a_test_client():
    got = _received({"method": "GET", "path": "/x", "headers": {"Authorization": "Bearer b"}})
    ua = got["user-agent"]
    assert ua.startswith("Mozilla/5.0") and "httpx" not in ua.lower() and "testclient" not in ua.lower()
    assert got["accept-language"].startswith("es") and "text/html" in got["accept"]
    assert got["authorization"] == "Bearer b"  # los headers del ataque se suman encima


def test_the_profile_is_not_fixed_across_runs():
    seen = {_received({"method": "GET", "path": "/x"}, random.Random(seed))["user-agent"] for seed in range(20)}
    assert len(seen) > 1 and seen <= {p["User-Agent"] for p in holdout.BROWSER_PROFILES}


def test_an_attack_can_still_choose_its_own_user_agent():
    got = _received({"method": "GET", "path": "/x", "headers": {"User-Agent": "curl/8"}})
    assert got["user-agent"] == "curl/8"


def test_base_tests_use_the_same_profile_as_the_attacks(monkeypatch):
    """Los clientes httpx que crean los tests (el del conftest del repo) mandan el perfil
    de la corrida; lo que fije el test se respeta."""
    import httpx

    from cachai import pytest_http
    profile = holdout.browser_headers(random.Random(1))
    monkeypatch.setattr(httpx.Client, "__init__", httpx.Client.__init__)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", httpx.AsyncClient.__init__)
    pytest_http.use_default_headers(profile)
    with httpx.Client() as c:
        assert c.headers["User-Agent"] == profile["User-Agent"] and c.headers["Accept-Language"] == profile["Accept-Language"]
    with httpx.Client(headers={"User-Agent": "mi-cliente", "Accept": "application/json"}) as c:
        assert c.headers["User-Agent"] == "mi-cliente" and c.headers["Accept"] == "application/json"
        assert c.headers["Accept-Language"] == profile["Accept-Language"]


def test_evaluator_shares_one_profile_between_tests_and_attacks(monkeypatch, tmp_path):
    from cachai import evaluator
    seen = {}
    monkeypatch.setattr(evaluator, "wait_for", lambda *a: True)
    monkeypatch.setattr(evaluator, "run_suite", lambda url, root, target, budget, headers=None: seen.setdefault("suite", headers) and {
        "complete": True, "ok": True})
    monkeypatch.setattr(evaluator, "load_attacks", lambda f: [])

    class Rep:
        def to_dict(self):
            return {"complete": True, "confirmed": []}
    monkeypatch.setattr(evaluator, "run_attacks", lambda url, attacks, deadline, headers=None: seen.setdefault("attacks", headers) and Rep())
    evaluator.evaluate("http://app", "/health", tmp_path, "tests", None, 30, tmp_path / "a.json")
    assert seen["suite"] == seen["attacks"] and seen["suite"]["User-Agent"].startswith("Mozilla/5.0")
