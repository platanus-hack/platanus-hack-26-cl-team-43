"""Fase 2: ejecución de ataques por HTTP, evidencia de ataques y selección por PR (sin Docker)."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from contraprueba import pipeline
from contraprueba.case import load_case
from contraprueba.gate import decide
from contraprueba.holdout import Attack, Predicate, run_attacks

FACTURAS = Path(__file__).resolve().parent.parent / "cases" / "facturas"


class Handler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, dict, bytes]] = []

    def _reply(self, status: int, body: str = "", headers: dict | None = None) -> None:
        data = body.encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _handle(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        Handler.seen.append((self.command, self.path, dict(self.headers), self.rfile.read(n)))
        if self.path == "/open":
            self._reply(200, "secreto FAC-A-001")
        elif self.path == "/denied":
            self._reply(404, "no existe")
        elif self.path == "/denied-with-secret":
            self._reply(403, "FAC-A-001")
        elif self.path == "/redirect":
            self._reply(302, "", {"Location": "/open"})
        elif self.path == "/echo":
            self._reply(200, "ok")
        else:
            self._reply(500, "boom")

    do_GET = do_POST = do_HEAD = _handle

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def server():
    Handler.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def attack(id: str, path: str, predicate: Predicate | None = None, method: str = "GET", **extra) -> Attack:
    return Attack(
        id, "authz", {"method": method, "path": path, **extra},
        predicate or Predicate(status_in=(200,), body_contains=("FAC-A-001",)),
    )


def test_truncated_body_without_match_fails_closed(server, monkeypatch):
    """Un cuerpo más largo que MAX_BODY que no coincide no se da por bloqueado: queda error."""
    import contraprueba.holdout as h
    monkeypatch.setattr(h, "MAX_BODY", 3)  # "secreto FAC-A-001" se corta antes del marcador
    report = run_attacks(server, [attack("grande", "/open")])
    r = report.results[0]
    assert r.outcome == "error" and r.http_status is None
    assert not report.complete  # fuerza evaluación incompleta -> rechazo


def test_confirmed_and_blocked_depend_on_status_and_body(server):
    report = run_attacks(server, [
        attack("abierto", "/open"),
        attack("negado", "/denied"),
        attack("negado-con-cuerpo", "/denied-with-secret"),
        attack("error-500", "/boom"),
    ])
    outcomes = {r.id: (r.outcome, r.http_status) for r in report.results}
    assert outcomes == {
        "abierto": ("confirmed", 200),
        "negado": ("blocked", 404),
        "negado-con-cuerpo": ("blocked", 403),
        "error-500": ("blocked", 500),
    }
    assert report.confirmed == ["abierto"] and report.complete
    assert report.to_dict()["expected"] == ["abierto", "negado", "negado-con-cuerpo", "error-500"]


def test_redirect_is_not_followed(server):
    report = run_attacks(server, [attack("redireccion", "/redirect")])
    assert [(r.outcome, r.http_status) for r in report.results] == [("blocked", 302)]
    assert [s[1] for s in Handler.seen] == ["/redirect"]


def test_redirect_status_can_be_the_confirmation(server):
    report = run_attacks(server, [attack("redireccion", "/redirect", Predicate(status_in=(302,)))])
    assert report.confirmed == ["redireccion"]


def test_sends_headers_and_json_body(server):
    run_attacks(server + "/", [attack("post", "/echo", method="POST", headers={"Authorization": "Bearer tok-bob"}, json={"a": 1})])
    method, path, headers, body = Handler.seen[0]
    assert (method, path) == ("POST", "/echo")
    assert headers["Authorization"] == "Bearer tok-bob" and headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"a": 1}


def test_connection_refused_is_error_not_blocked():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    report = run_attacks(f"http://127.0.0.1:{port}", [attack("caido", "/open")], timeout=2)
    (r,) = report.results
    assert r.outcome == "error" and r.error and r.http_status is None
    assert not report.complete and report.confirmed == []


def test_expired_deadline_sends_nothing(server):
    report = run_attacks(server, [attack("a", "/open"), attack("b", "/open")], deadline=time.monotonic() - 1)
    assert [r.outcome for r in report.results] == ["error", "error"]
    assert Handler.seen == [] and not report.complete


# --- evidencia ----------------------------------------------------------------------

def suite(**over):
    s = {"complete": True, "timed_out": False, "exit_code": 0, "total": 1, "passed": 1, "failed": [], "failures": [], "skipped": [], "ok": True}
    s.update(over)
    return s


def result(status="ok", attacks=None):
    return {"schema": 1, "status": status, "errors": [], "suites": {"base": suite(), "holdout": None}, "attacks": attacks}


def test_expected_attacks_without_result_are_incomplete():
    ev = pipeline.evidence_from(result("ok", attacks=None), [], 0, expect_attacks=True)
    assert ev.incomplete and not decide(ev).approved


def test_attacks_not_expected_and_absent_do_not_block():
    ev = pipeline.evidence_from(result("ok", attacks=None), [], 0)
    assert not ev.incomplete and decide(ev).approved


def test_confirmed_attack_rejects_with_its_id():
    report = {"expected": ["authz-pdf"], "results": [], "confirmed": ["authz-pdf"], "errors": [], "complete": True}
    ev = pipeline.evidence_from(result("failed", attacks=report), [], 1, expect_attacks=True)
    v = decide(ev)
    assert not v.approved and any("authz-pdf" in r for r in v.reasons)


def test_blocked_attacks_approve():
    report = {"expected": ["a"], "results": [], "confirmed": [], "errors": [], "complete": True}
    ev = pipeline.evidence_from(result("ok", attacks=report), [], 0, expect_attacks=True)
    assert decide(ev).approved


def test_bot_mode_requires_attacks():
    with pytest.raises(ValueError):
        pipeline.check(".", "main", "main", mode="bot")


# --- selección por PR ---------------------------------------------------------------

def test_attacks_for_picks_visible_or_reserved_by_pr_kind():
    case = load_case(FACTURAS)
    visible = {a.id for a in case.attacks_for("parche-a-medias", "bot")}
    assert visible == {"authz-invoice-1", "authz-invoice-2", "authz-invoice-reverse"}
    for mode in ("full", "ci", "restore"):
        reserved = {a.id for a in case.attacks_for("parche-correcto", mode)}
        assert len(reserved) == 7 and "authz-pdf" in reserved and not reserved & visible
    for pr in ("honesto", "trampa-conftest", "trampa-paquete"):
        assert case.attacks_for(pr, "bot") is None and case.attacks_for(pr, "full") is None
