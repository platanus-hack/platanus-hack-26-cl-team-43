"""Medición: intervalos y resumen por modo."""

import json

import pytest

from cachai import bench


def test_wilson_known_values():
    assert bench.wilson(0, 0) == (0.0, 1.0)
    low, high = bench.wilson(8, 8)
    assert round(low, 2) == 0.68 and high == 1.0
    low, high = bench.wilson(0, 8)
    assert low == 0.0 and round(high, 2) == 0.32
    low, high = bench.wilson(5, 10)
    assert round(low, 2) == 0.24 and round(high, 2) == 0.76


def row(pr, kind, mode, approved, outcome=None):
    should = kind in ("honest", "correct_patch")
    outcome = outcome or ("approved" if approved else "rejected")
    return {"case": "c", "pr": pr, "kind": kind, "description": "d", "should_be_approved": should, "mode": mode,
            "approved": approved, "outcome": outcome, "correct": outcome == ("approved" if should else "rejected"), "seconds": 1.0,
            "result": {"reasons": [] if approved else ["motivo"], "notes": []}}


ROWS = [
    row("t1", "cheat", "ci", True), row("t1", "cheat", "full", False),
    row("t2", "cheat", "ci", True), row("t2", "cheat", "full", False),
    row("p1", "half_patch", "ci", True), row("p1", "half_patch", "full", False),
    row("p2", "breaking_patch", "ci", True), row("p2", "breaking_patch", "full", True),
    row("h1", "honest", "ci", True), row("h1", "honest", "full", False),
    row("h2", "correct_patch", "ci", True), row("h2", "correct_patch", "full", True),
]


def test_summary_counts_rejections_per_group():
    s = bench.summarize(ROWS)
    assert s["ci"]["trampas"] == {"n": 2, "rejected": 0, "approved": 2, "incomplete": 0, "rate": 0.0,
                                  "ci95": s["ci"]["trampas"]["ci95"], "wrong": ["t1", "t2"]}
    assert (s["full"]["trampas"]["rejected"], s["full"]["trampas"]["wrong"]) == (2, [])
    assert (s["full"]["parches_malos"]["rejected"], s["full"]["parches_malos"]["wrong"]) == (1, ["p2"])
    # En los legítimos, un rechazo es una falsa alarma.
    assert (s["full"]["legitimos"]["rejected"], s["full"]["legitimos"]["wrong"]) == (1, ["h1"])


def test_every_kind_belongs_to_exactly_one_group():
    from cachai.case import PR_KINDS
    kinds = [k for g in bench.GROUPS.values() for k in g["kinds"]]
    assert sorted(kinds) == sorted(PR_KINDS)


def test_write_outputs(tmp_path):
    summary = bench.write(ROWS, [], tmp_path)
    data = json.loads((tmp_path / "bench.json").read_text())
    assert data["summary"] == summary and len(data["runs"]) == len(ROWS)
    assert "result" not in data["runs"][0] and data["runs"][1]["reasons"] == ["motivo"]
    assert (tmp_path / "runs" / "c" / "t1" / "full.json").is_file()
    md = (tmp_path / "resultados.md").read_text()
    assert "| `p2` | breaking_patch | rechazar | **aprobado** | **aprobado** |" in md
    assert "2/2 (34% a 100%)" in md


def test_find_cases():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "cases"
    assert [c.id for c in bench.find_cases(root)] == ["facturas", "secretos"]
    assert [c.id for c in bench.find_cases(root / "facturas")] == ["facturas"]


def test_incomplete_evaluation_is_not_a_detection_nor_a_false_alarm():
    """Si la infraestructura falla, el PR se rechaza, pero eso no cuenta como haberlo detectado."""
    rows = [
        row("t1", "cheat", "full", False, "incomplete"), row("t2", "cheat", "full", False),
        row("h1", "honest", "full", False, "incomplete"), row("h2", "honest", "full", True),
    ]
    s = bench.summarize(rows)["full"]
    assert (s["trampas"]["rejected"], s["trampas"]["incomplete"], s["trampas"]["wrong"]) == (1, 1, ["t1"])
    assert (s["legitimos"]["rejected"], s["legitimos"]["incomplete"], s["legitimos"]["wrong"]) == (0, 1, ["h1"])
    md = bench.to_markdown(rows, bench.summarize(rows), [])
    assert "1/2 (10% a 90%; 1 sin veredicto)" in md and "| **sin veredicto** |" in md


def test_gate_outcome_separates_evidence_from_missing_evidence():
    from cachai.gate import Evidence, decide
    assert decide(Evidence(True)).outcome == "approved"
    assert decide(Evidence(False, base_tests_failed=["t — assert"])).outcome == "rejected"
    assert decide(Evidence(False, incomplete=["tests de la base: no terminaron (tiempo agotado)"])).outcome == "incomplete"
    assert decide(Evidence(True, incomplete=["ataques sin veredicto"], holdout_tests_failed=["t"])).outcome == "rejected"
    v = decide(Evidence(False, incomplete=["la app no respondió"]))
    assert not v.approved and len(v.reasons) == 1


def test_baseline_timeout_is_incomplete():
    from cachai import pipeline
    r = pipeline._baseline("ci", "a" * 40, "b" * 40, {"passed": False, "timed_out": True, "exit_code": None, "log_tail": ""})
    assert r.outcome == "incomplete" and not r.approved
    assert pipeline._baseline("ci", "a" * 40, "b" * 40, {"passed": False, "timed_out": False, "exit_code": 1, "log_tail": ""}).outcome == "rejected"
