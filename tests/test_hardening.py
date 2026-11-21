"""Regresiones de la revisión de la fase 0: contratos estrictos y nada de verdes falsos."""

import json
import os
from pathlib import Path

import pytest

from contraprueba.case import load_case, materialize, safe_relpath
from contraprueba.evaluator import overall_status, run_suite, summarize
from contraprueba.gate import Evidence, decide
from contraprueba.holdout import Attack, AttackReport, AttackResult, Predicate, split, split_record

OK = {"status_in": [200]}
REQ = {"method": "GET", "path": "/x"}


# --- Predicate y Attack -----------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"body_contains": "FAC-A-001"},           # texto donde va lista
    {"status_in": 200},
    {"status_in": [True]},
    {"status_in": [99]},
    {"status_in": ["200"]},
    {"status_in": [200], "body_contains": [""]},
    {"status_in": [200], "body_contains": [None]},
    {"status_in": [200], "body_not_contains": "x"},
    {"body_contains_any": "FAC"},
    {"body_contains_any": [""]},
    None,
    [],
])
def test_predicate_rejects_bad_types(bad):
    with pytest.raises(ValueError):
        Predicate.from_dict(bad)


def _attack(**over):
    d = {"id": "a1", "family": "authz", "request": dict(REQ), "succeeded_if": dict(OK)}
    d.update(over)
    return d


@pytest.mark.parametrize("bad", [
    _attack(extra=1),
    {k: v for k, v in _attack().items() if k != "succeeded_if"},
    _attack(id="A B"),
    _attack(id=""),
    _attack(request={"path": "/x"}),
    _attack(request={"method": "TRACE", "path": "/x"}),
    _attack(request={"method": "GET", "path": "//evil.invalid/x"}),
    _attack(request={"method": "GET", "path": "http://evil.invalid/"}),
    _attack(request={"method": "GET", "path": "/x\r\nHost: y"}),
    _attack(request={"method": "GET", "path": "/x", "headers": {"Host": "evil"}}),
    _attack(request={"method": "GET", "path": "/x", "headers": {"A": 1}}),
    _attack(request={"method": "GET", "path": "/x", "cookies": {}}),
    _attack(request="GET /x"),
])
def test_attack_rejects_malformed(bad):
    with pytest.raises((ValueError, KeyError)):
        Attack.from_dict(bad)


def test_attack_path_with_query_and_encoding_is_valid():
    Attack.from_dict(_attack(request={"method": "GET", "path": "/a?name=..%2Fb&u=http://x"}))


# --- Selección visible / reservada -------------------------------------------

def _attacks(n):
    return [Attack.from_dict(_attack(id=f"a{i}")) for i in range(n)]


def test_split_always_reserves_at_least_one():
    for n in (2, 3, 10):
        for shown in (0.1, 0.5, 0.99):
            vis, res = split(_attacks(n), shown=shown)
            assert vis and res and len(vis) + len(res) == n


def test_split_rejects_too_few_or_bad_ratio():
    with pytest.raises(ValueError):
        split(_attacks(1))
    with pytest.raises(ValueError):
        split([])
    with pytest.raises(ValueError):
        split(_attacks(4), shown=1.0)


def test_split_record_is_reproducible_and_disjoint():
    r1, r2 = split_record(_attacks(10), seed=7), split_record(_attacks(10), seed=7)
    assert r1 == r2
    assert not set(r1.visible) & set(r1.reserved)
    assert json.loads(json.dumps(r1.to_dict()))["seed"] == 7


# --- Completitud de ataques y decisión ----------------------------------------

def test_attack_report_error_is_not_blocked():
    rep = AttackReport(("a", "b"), [AttackResult("a", "blocked", 403), AttackResult("b", "error", error="timeout")])
    assert not rep.complete and rep.errors == ["b"] and rep.confirmed == []


def test_attack_report_missing_result_is_incomplete():
    assert not AttackReport(("a", "b"), [AttackResult("a", "blocked", 403)]).complete
    assert not AttackReport(("a",), []).complete
    assert AttackReport(("a",), [AttackResult("a", "blocked", 403)]).complete


def test_gate_rejects_incomplete_evaluation():
    v = decide(Evidence(base_tests_passed=True, incomplete=["ataques: 1 sin ejecutar"]))
    assert not v.approved and "incompleta" in v.reasons[0]


# --- Política de tests omitidos y estado global --------------------------------

def _case(outcome):
    return {"nodeid": f"t::{outcome}", "outcome": outcome}


def test_all_skipped_is_incomplete_not_ok():
    s = summarize([_case("skipped")], 0, False, "")
    assert not s["complete"] and not s["ok"]


def test_partial_skip_is_not_ok():
    s = summarize([_case("passed"), _case("skipped")], 0, False, "")
    assert s["complete"] and not s["ok"] and s["skipped"]


def test_timeout_is_incomplete():
    s = summarize([_case("passed")], None, True, "")
    assert not s["complete"] and not s["ok"]


def test_overall_status_distinguishes_failed_from_incomplete():
    good = summarize([_case("passed")], 0, False, "")
    bad = summarize([_case("failed")], 1, False, "")
    skipped = summarize([_case("skipped")], 0, False, "")
    base = {"errors": [], "attacks": None}
    assert overall_status({**base, "suites": {"base": good, "holdout": None}}) == "ok"
    assert overall_status({**base, "suites": {"base": bad, "holdout": None}}) == "failed"
    assert overall_status({**base, "suites": {"base": skipped, "holdout": None}}) == "incomplete"
    assert overall_status({**base, "suites": {"base": None, "holdout": None}}) == "incomplete"
    assert overall_status({**base, "errors": ["app caída"], "suites": {"base": good}}) == "incomplete"
    confirmed = AttackReport(("a",), [AttackResult("a", "confirmed", 200)]).to_dict()
    assert overall_status({**base, "attacks": confirmed, "suites": {"base": good}}) == "failed"
    errored = AttackReport(("a",), [AttackResult("a", "error", error="timeout")]).to_dict()
    assert overall_status({**base, "attacks": errored, "suites": {"base": good}}) == "incomplete"


def _suite(tmp_path, body):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(body)
    return run_suite("http://127.0.0.1:9", tmp_path, "tests", 60)


def test_run_suite_fully_skipped_suite_is_not_green(tmp_path):
    s = _suite(tmp_path, "import pytest\n@pytest.mark.skip(reason='no')\ndef test_s():\n    assert False\n")
    assert not s["ok"] and not s["complete"]


def test_run_suite_xfail_is_not_green(tmp_path):
    s = _suite(tmp_path, "import pytest\ndef test_a():\n    pass\n@pytest.mark.xfail\ndef test_b():\n    assert False\n")
    assert not s["ok"]


def test_run_suite_timeout_is_reported(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("import time\ndef test_slow():\n    time.sleep(30)\n")
    s = run_suite("http://127.0.0.1:9", tmp_path, "tests", 2)
    assert s["timed_out"] and not s["complete"] and not s["ok"]


# --- Casos: confinamiento --------------------------------------------------

@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../x", ".git/config", "a/.git/hooks/pre-commit", "", "a\\b"])
def test_safe_relpath_rejects(bad):
    with pytest.raises(ValueError):
        safe_relpath(bad, "t")


def _write_case(root: Path, prs, **extra):
    (root / "base" / "tests").mkdir(parents=True)
    (root / "base" / "app.py").write_text("x = 1\n")
    (root / "base" / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (root / "case.json").write_text(json.dumps({"id": "t", "app": {"module": "app:app"}, "prs": prs, **extra}))


def test_delete_outside_repo_is_rejected_and_nothing_is_deleted(tmp_path):
    victim = tmp_path / "outside.txt"
    victim.write_text("no me borres")
    _write_case(tmp_path / "case", [{"name": "x", "kind": "cheat", "delete": ["../outside.txt"]}])
    with pytest.raises(ValueError):
        load_case(tmp_path / "case")
    assert victim.exists()


@pytest.mark.parametrize("name", ["main", "../x", "A", "a b", "-x"])
def test_bad_branch_names_are_rejected(tmp_path, name):
    _write_case(tmp_path, [{"name": name, "kind": "honest", "delete": ["app.py"]}])
    with pytest.raises(ValueError):
        load_case(tmp_path)


def test_overlay_with_git_metadata_is_rejected(tmp_path):
    case_dir = tmp_path / "case"
    _write_case(case_dir, [{"name": "x", "kind": "cheat"}])
    hooks = case_dir / "prs" / "x" / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\ntouch /tmp/pwned\n")
    with pytest.raises(ValueError):
        materialize(load_case(case_dir), tmp_path / "repo")


def test_overlay_with_symlink_is_rejected(tmp_path):
    case_dir = tmp_path / "case"
    _write_case(case_dir, [{"name": "x", "kind": "cheat"}])
    (case_dir / "prs" / "x").mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    os.symlink(secret, case_dir / "prs" / "x" / "leak.txt")
    with pytest.raises(ValueError):
        materialize(load_case(case_dir), tmp_path / "repo")


@pytest.mark.parametrize("app", [
    {"module": "app"},
    {"module": "app:app; rm -rf /"},
    {"module": "app:app", "port": 0},
    {"module": "app:app", "port": True},
    {"module": "app:app", "health": "health"},
])
def test_bad_app_spec_is_rejected(tmp_path, app):
    _write_case(tmp_path, [])
    d = json.loads((tmp_path / "case.json").read_text())
    d["app"] = app
    (tmp_path / "case.json").write_text(json.dumps(d))
    with pytest.raises((ValueError, TypeError)):
        load_case(tmp_path)


def test_case_reads_non_default_fields(tmp_path):
    _write_case(tmp_path, [], holdout_tests="reservados", split={"shown": 0.3, "seed": 5})
    (tmp_path / "reservados").mkdir()
    d = json.loads((tmp_path / "case.json").read_text())
    d["app"] = {"module": "app:app", "port": 9123, "health": "/ping"}
    (tmp_path / "case.json").write_text(json.dumps(d))
    c = load_case(tmp_path)
    assert (c.app.port, c.app.health, c.split_shown, c.split_seed) == (9123, "/ping", 0.3, 5)
    assert c.holdout_tests_path == tmp_path.resolve() / "reservados"


def test_missing_holdout_dir_is_rejected(tmp_path):
    _write_case(tmp_path, [], holdout_tests="no-existe")
    with pytest.raises(ValueError):
        load_case(tmp_path)


# --- Ronda 2: plazos e invariantes del reporte de ataques ------------------------

def test_attack_report_rejects_empty_or_duplicate_expected():
    with pytest.raises(ValueError):
        AttackReport((), [])
    with pytest.raises(ValueError):
        AttackReport(("a", "a"), [])


def test_attack_report_duplicate_results_are_incomplete():
    rep = AttackReport(("a", "b"), [AttackResult("a", "blocked", 403), AttackResult("a", "blocked", 403)])
    assert not rep.complete


def test_attack_report_extra_result_is_incomplete():
    rep = AttackReport(("a",), [AttackResult("a", "blocked", 403), AttackResult("z", "blocked", 403)])
    assert not rep.complete


@pytest.mark.parametrize("kw", [
    {"outcome": "blocked", "http_status": None},
    {"outcome": "blocked", "http_status": 403, "error": "timeout"},
    {"outcome": "confirmed", "http_status": True},
    {"outcome": "confirmed", "http_status": 700},
    {"outcome": "error"},
])
def test_attack_result_rejects_contradictions(kw):
    with pytest.raises(ValueError):
        AttackResult("a", **kw)


@pytest.mark.parametrize("budget", [0, -1, float("inf"), float("nan")])
def test_evaluate_rejects_bad_budget(tmp_path, budget):
    from contraprueba.evaluator import evaluate
    with pytest.raises(ValueError):
        evaluate("http://127.0.0.1:9", "/h", tmp_path, "tests", None, budget)


def test_run_suite_without_budget_does_not_start(tmp_path):
    s = run_suite("http://127.0.0.1:9", tmp_path, "tests", 0)
    assert s["timed_out"] and not s["complete"] and not s["ok"]


class _SlowHealth:
    """Servidor local cuya ruta de salud responde después de `delay` segundos."""

    def __init__(self, delay):
        import http.server
        import threading

        delay_ = delay

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                import time
                time.sleep(delay_)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def test_late_health_response_does_not_count(tmp_path):
    from contraprueba.evaluator import evaluate
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_a():\n    pass\n")
    srv = _SlowHealth(1.5)
    try:
        r = evaluate(srv.url, "/ping", tmp_path, "tests", None, 0.2)
    finally:
        srv.close()
    assert r["status"] == "incomplete" and r["suites"]["base"] is None and r["errors"]


def test_holdout_without_budget_left_is_incomplete(tmp_path, monkeypatch):
    from contraprueba import evaluator
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("import time\ndef test_a():\n    time.sleep(1.2)\n")
    (tmp_path / "reservados").mkdir()
    (tmp_path / "reservados" / "test_r.py").write_text("def test_r():\n    pass\n")
    srv = _SlowHealth(0)
    try:
        r = evaluator.evaluate(srv.url, "/ping", tmp_path, "tests", tmp_path / "reservados", 1.0)
    finally:
        srv.close()
    assert r["status"] == "incomplete"
    assert r["suites"]["holdout"]["timed_out"] and not r["suites"]["holdout"]["ok"]
