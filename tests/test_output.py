"""Salida de la CLI: texto plano estable y versión en colores para la demo."""

import pytest

from contraprueba import pipeline


def result(approved, reasons=(), **kw):
    return pipeline.CheckResult("full", "a" * 40, "b" * 40, approved, list(reasons), **kw)


def test_plain_text_lists_every_reason():
    text = result(False, [f"motivo {i}" for i in range(9)]).to_text()
    assert text.startswith("RECHAZADO  (modo full") and text.count("\n  - ") == 9 and "\033" not in text


def test_color_output_shows_a_block_and_caps_the_list():
    text = result(False, [f"motivo {i}" for i in range(9)]).to_text(color=True)
    assert "\033[1;97;41m  RECHAZADO  \033[0m" in text
    assert text.count("✗") == pipeline.MAX_SHOWN and "y 3 motivos más" in text


SUITE = {"passed": 12, "ok": True}


def evaluator(attacks=None, holdout=None, status="ok", base=SUITE):
    return {"status": status, "suites": {"base": base, "holdout": holdout}, "attacks": attacks}


def blocked(*ids):
    return {"complete": True, "results": [{"id": i, "outcome": "blocked", "http_status": 404} for i in ids]}


SPLIT = {"visible": ["v"], "reserved": ["r1", "r2"]}


def test_approved_says_what_was_checked():
    r = result(True, evaluator=evaluator(blocked("v", "r1", "r2"), {"passed": 9, "ok": True}), split=SPLIT)
    assert r.checked() == ["12 tests de la rama base pasan desde otro contenedor", "9 tests funcionales reservados pasan",
                           "3 ataques bloqueados, 2 de ellos reservados"]
    text = r.to_text(color=True)
    assert "\033[1;97;42m  APROBADO  \033[0m" in text and text.count("✓") == 3
    visible_only = result(True, evaluator=evaluator(blocked("v")), split=SPLIT)
    assert visible_only.checked()[-1] == "1 ataques bloqueados (solo los que vio el parchador)"
    assert result(True, evaluator=evaluator()).checked() == ["12 tests de la rama base pasan desde otro contenedor"]


def test_rejected_run_claims_nothing():
    assert result(False, ["motivo"], evaluator=evaluator(blocked("v")), split=SPLIT).checked() == []
    assert pipeline.checked_lines({"approved": True, "outcome": "incomplete", "evaluator": evaluator()}) == []


@pytest.mark.parametrize("ev", [
    evaluator(status="failed"),
    evaluator(base={"passed": 12, "ok": False}),
    evaluator(base=None),
    evaluator(holdout={"passed": 9, "ok": False}),
    evaluator({"complete": True, "results": [{"id": "v", "outcome": "confirmed"}]}),
    evaluator({"complete": True, "results": [{"id": "v", "outcome": "error"}]}),
    evaluator({"complete": False, "results": [{"id": "v", "outcome": "blocked"}]}),
    evaluator({"complete": True, "results": []}),
])
def test_approved_report_whose_detail_does_not_add_up_is_an_error(ev):
    """Un aprobado no puede afirmar comprobaciones que su propio reporte contradice."""
    with pytest.raises(ValueError):
        pipeline.checked_lines({"approved": True, "outcome": "approved", "inside": None, "evaluator": ev, "split": SPLIT})


def test_baseline_report_must_have_passed():
    with pytest.raises(ValueError):
        pipeline.checked_lines({"approved": True, "outcome": "approved", "inside": {"passed": False}})


def test_baseline_says_where_pytest_ran():
    r = pipeline._baseline("ci", "a" * 40, "b" * 40, {"passed": True, "timed_out": False, "exit_code": 0, "log_tail": ""})
    assert r.checked() == ["pytest salió con 0 dentro del mismo contenedor que el código"]


# --- CLI: --report -----------------------------------------------------------

@pytest.fixture
def fake_run(monkeypatch):
    from contraprueba import cli
    calls = []

    def run_case(case, prs, modes, budget=120.0):
        calls.append((prs, modes))
        return {(pr, m): result(True, evaluator=evaluator()) for pr in prs for m in modes}

    monkeypatch.setattr(cli, "run_case", run_case)
    return calls


@pytest.mark.parametrize("extra", [[], ["--json"], ["--color", "always"]])
def test_case_report_is_written_whatever_the_output_format(fake_run, tmp_path, capsys, extra):
    import json
    from contraprueba import cli
    report = tmp_path / "r.json"
    code = cli.main(["case", "cases/facturas", "--pr", "honesto", "--mode", "full", "--report", str(report), *extra])
    assert code == 0 and json.loads(report.read_text())["outcome"] == "approved"


def test_case_report_needs_a_single_run(fake_run, tmp_path, capsys):
    from contraprueba import cli
    report = tmp_path / "r.json"
    assert cli.main(["case", "cases/facturas", "--pr", "honesto", "--report", str(report)]) == 2
    assert cli.main(["case", "cases/facturas", "--mode", "full", "--report", str(report)]) == 2
    assert not report.exists() and fake_run == []
    assert "una sola corrida" in capsys.readouterr().err
