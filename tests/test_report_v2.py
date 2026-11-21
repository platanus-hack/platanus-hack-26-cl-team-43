"""Reporte v2: esquema publicado, motivos tipados y nada del material reservado en lo público."""

import json
from pathlib import Path

import jsonschema
import pytest

from cachai import cli, pipeline
from cachai.case import load_case
from cachai.evaluator import SCHEMA as EVAL_SCHEMA
from cachai.gate import REASON_KINDS, decide

ROOT = Path(__file__).resolve().parent.parent
FACTURAS = ROOT / "cases" / "facturas"
SCHEMA = json.loads((ROOT / "schema" / "report.schema.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
BASE, PR = "a" * 40, "b" * 40


def suite(ok=True, failures=()):
    failures = list(failures)
    return {"complete": True, "timed_out": False, "exit_code": 0 if ok else 1, "total": 3, "passed": 3 - len(failures),
            "failed": [f["nodeid"] for f in failures], "failures": failures, "skipped": [], "ok": ok}


def run(confirmed: str = "all", holdout_fail: bool = True):
    """Corrida `full` simulada sobre facturas: el evaluador confirma los ataques pedidos."""
    case = load_case(FACTURAS)
    plan = pipeline.AttackPlan(tuple(case.attacks()), case.split())
    ids = [a.id for a in plan.attacks]
    hits = set(ids) if confirmed == "all" else set()
    holdout_failures = [{"nodeid": "reservados.test_funcional::test_adjunto_en_subcarpeta", "file": "test_funcional.py",
                         "line": 30, "message": "assert 'Contrato de servicios' in body"}] if holdout_fail else []
    result = {
        "schema": EVAL_SCHEMA,
        "status": "failed" if hits or holdout_fail else "ok",
        "errors": [],
        "suites": {"base": suite(), "holdout": suite(not holdout_fail, holdout_failures)},
        "attacks": {"complete": True, "results": [
            {"id": i, "outcome": "confirmed" if i in hits else "blocked", "http_status": 200 if i in hits else 404} for i in ids]},
    }
    ev = pipeline.evidence_from(result, [], 1 if result["status"] == "failed" else 0, True, plan, tuple(ids))
    v = decide(ev)
    r = pipeline.CheckResult("full", BASE, PR, v.approved, v.reasons, v.notes, [], evaluator=result,
                             split=plan.split.to_dict(), outcome=v.outcome, typed=v.typed,
                             secrets=pipeline.reserved_secrets(plan, case.holdout_tests_path, result))
    return r, plan


def test_rejected_report_validates_and_types_every_reason():
    r, _ = run()
    rep = r.report()
    VALIDATOR.validate(rep)
    kinds = {x["kind"] for x in rep["reasons"]}
    assert kinds == {"attack_visible", "attack_hidden", "holdout_test"} and kinds <= set(REASON_KINDS)
    hidden = [x for x in rep["reasons"] if x["kind"] == "attack_hidden"]
    assert hidden and all(x["family"] in ("authz", "files") and x["route"].startswith("GET /") for x in hidden)
    assert rep["public"]["markdown"].startswith("### Cachai: RECHAZADO")


def test_nothing_from_reserved_material_reaches_the_public_report():
    r, plan = run()
    text = json.dumps(r.report(), ensure_ascii=False)
    for a in plan.subset(plan.split.reserved):
        assert a.request["path"] not in text, a.id
        assert a.id not in text
        for marker in a.succeeded_if.body_contains + a.succeeded_if.body_contains_any:
            assert marker not in text, marker
    for leaked in ("test_adjunto_en_subcarpeta", "test_funcional", "Contrato de servicios"):
        assert leaked not in text
    # El detalle completo sigue en el reporte interno, para quien tiene el material.
    full = json.dumps(r.to_dict(), ensure_ascii=False)
    assert any(a.id in full for a in plan.subset(plan.split.reserved))


def test_public_route_hides_ids_files_and_query():
    case = load_case(FACTURAS)
    routes = {a.public_route() for a in case.attacks()}
    assert all("?" not in x and ".." not in x and "FAC" not in x for x in routes)
    assert "GET /invoices/{…}" in routes


def test_approved_report_says_what_was_checked():
    r, _ = run(confirmed="none", holdout_fail=False)
    rep = r.report()
    VALIDATOR.validate(rep)
    assert rep["approved"] and rep["reasons"] == [] and rep["checked"]
    assert rep["public"]["summary"].startswith("Cachai: APROBADO")


def test_cli_report_writes_v2(tmp_path, monkeypatch):
    r, _ = run()
    monkeypatch.setattr(pipeline, "check", lambda *a, **k: r)
    out = tmp_path / "r.json"
    assert cli.main(["check", "--pr", "x", "--report", str(out), "--color", "never"]) == 1
    rep = json.loads(out.read_text())
    assert rep["schema_version"] == 2
    VALIDATOR.validate(rep)


@pytest.mark.parametrize("approved, outcome, reasons", [(True, "rejected", []), (False, "rejected", [])])
def test_schema_rejects_contradictions(approved, outcome, reasons):
    r, _ = run(confirmed="none", holdout_fail=False)
    rep = r.report() | {"approved": approved, "outcome": outcome, "reasons": reasons}
    assert not VALIDATOR.is_valid(rep)


def test_cli_public_output_hides_reserved_detail(monkeypatch, capsys):
    r, plan = run()
    monkeypatch.setattr(pipeline, "check", lambda *a, **k: r)
    assert cli.main(["check", "--pr", "x", "--public"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("### Cachai: RECHAZADO")
    for a in plan.subset(plan.split.reserved):
        assert a.id not in out and a.request["path"] not in out


def test_action_and_example_workflow_are_valid_yaml():
    yaml = pytest.importorskip("yaml")
    action = yaml.safe_load((ROOT / "action" / "action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    step = next(s for s in action["runs"]["steps"] if s.get("id") == "check")
    assert "--public" in step["run"] and "pull_request_target" not in (ROOT / "examples" / "workflow.yml").read_text().split("#")[0]
    wf = yaml.safe_load((ROOT / "examples" / "workflow.yml").read_text())
    assert list(wf[True]) == ["pull_request"]  # `on:` se lee como True en YAML 1.1
