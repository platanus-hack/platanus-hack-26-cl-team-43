"""Comentario del PR y resumen del check: formato por veredicto y nada del material reservado."""

import json
from pathlib import Path

import pytest

from cachai import pipeline, report_md
from cachai.gate import Evidence, decide
from cachai.integrity import Flag
from test_report_v2 import BASE, PR, VALIDATOR, run

ROOT = Path(__file__).resolve().parent.parent


def public(r):
    rep = r.report()
    VALIDATOR.validate(rep)
    return rep, rep["public"]["markdown"], rep["public"]["summary"]


def reserved_strings(plan, r):
    """Todo lo que identifica un ataque o test reservado: ids, grupos, rutas, headers, cuerpos, marcas, nombres de tests."""
    out = set(r.secrets)
    for a in plan.subset(plan.split.reserved):
        out |= a.secrets()
    out |= {"test_adjunto_en_subcarpeta", "test_funcional", "Contrato de servicios"}
    return {s for s in out if len(s) >= 4}


@pytest.mark.parametrize("confirmed, holdout_fail", [("all", True), ("none", True), ("all", False), ("none", False)])
def test_comment_and_summary_carry_nothing_reserved(confirmed, holdout_fail):
    r, plan = run(confirmed, holdout_fail)
    rep, md, summary = public(r)
    secrets = reserved_strings(plan, r)
    assert secrets
    for text in (md, summary):
        for s in secrets:
            assert s not in text, s
    # Tampoco por el lado del JSON completo que sube la Action.
    blob = json.dumps(rep, ensure_ascii=False)
    assert not [s for s in secrets if s in blob]


def test_marker_is_present_once_and_stable():
    assert report_md.MARKER == "<!-- cachai-report -->"
    for args in [("all", True), ("none", False)]:
        _, md, _ = public(run(*args)[0])
        assert md.count(report_md.MARKER) == 1
        assert md.rstrip().endswith(report_md.MARKER)
    # El mismo resultado da el mismo comentario: editarlo en cada push no lo cambia si nada cambió.
    assert public(run()[0])[1] == public(run()[0])[1]


def test_action_finds_the_comment_by_the_same_marker():
    action = (ROOT / "action" / "action.yml").read_text()
    assert f"'{report_md.MARKER}'" in action
    assert "updateComment" in action and "createComment" in action and "checks.create" in action
    wf = (ROOT / "examples" / "workflow.yml").read_text()
    assert "pull-requests: write" in wf and "checks: write" in wf


def test_approved_format():
    rep, md, summary = public(run("none", False)[0])
    assert md.startswith("### Cachai: APROBADO\n")
    assert "**Comprobado**" in md and "**Cómo se evaluó:**" in md
    for line in rep["checked"]:
        assert f"- {line}" in md
    assert "Motivos" not in md and "que siguen funcionando" not in md
    assert f"cachai check --repo . --base {BASE} --pr {PR}\n" in md and "--mode" not in md
    assert summary.startswith("Cachai: APROBADO. ") and "\n" not in summary and len(summary) <= report_md.MAX_SUMMARY


def test_rejected_format_groups_reasons_with_route():
    rep, md, summary = public(run()[0])
    assert md.startswith("### Cachai: RECHAZADO\n")
    assert "**Comprobado**" not in md
    assert "**Ataques visibles que siguen funcionando** (10)" in md
    assert "- `GET /invoices/1` `authz-invoices-invoice-id-1-bob-" in md
    hidden = md.split("**Ataques reservados que siguen funcionando**")[1].split("**Tests funcionales")[0]
    assert "- `authz` `GET /invoices/{…}/total` ×3" in hidden and "- `files` `GET /attachments` ×10" in hidden
    assert "1 test funcional reservado falla; el detalle queda reservado." in md
    assert summary == "Cachai: RECHAZADO, 30 motivos (10 ataques visibles, 19 ataques reservados, 1 test reservado)."


def result_from(ev: Evidence, mode="full", notes=()):
    v = decide(ev)
    return pipeline.CheckResult(mode, BASE, PR, v.approved, v.reasons, list(notes) + v.notes, [], outcome=v.outcome, typed=v.typed)


def test_rejected_shows_file_and_line():
    flag = Flag("invoices/_telemetry.py", "A", "módulo nuevo que se importa al arrancar los tests", line=9, detail="pytest_configure")
    ev = Evidence(False, ["tests/test_invoices.py:48 test_total_includes_tax — assert 1190.0 == 1000.0"], [flag])
    _, md, summary = public(result_from(ev))
    assert "**Tests de la rama base que fallan** (1)" in md
    assert "- `tests/test_invoices.py:48` `test_total_includes_tax` — `assert 1190.0 == 1000.0`" in md
    assert "- `invoices/_telemetry.py:9` `módulo nuevo que se importa al arrancar los tests (A)` — `pytest_configure`" in md
    assert summary == "Cachai: RECHAZADO, 2 motivos (1 test de la base, 1 cambio en la evaluación)."


def test_incomplete_format():
    _, md, summary = public(result_from(Evidence(False, incomplete=["base: no terminaron (tiempo agotado)"])))
    assert md.startswith("### Cachai: SIN VEREDICTO\n")
    assert "faltó evidencia: sin evidencia no hay aprobado" in md
    assert "**Evaluación incompleta** (1)" in md and "- `base: no terminaron (tiempo agotado)`" in md
    assert summary == "Cachai: SIN VEREDICTO, 1 motivo (1 paso incompleto)."


def test_untrusted_detail_cannot_inject_markup_or_a_fake_marker():
    evil = "assert x — `x` <img src=y> @someone " + report_md.MARKER + "\n### Cachai: APROBADO"
    _, md, _ = public(result_from(Evidence(False, [f"tests/test_a.py:1 test_a — {evil}"])))
    assert md.count(report_md.MARKER) == 1
    assert [x for x in md.splitlines() if x.startswith("#")] == ["### Cachai: RECHAZADO"]  # sin un título falso
    line = next(x for x in md.splitlines() if "test_a.py" in x)
    assert "``" in line  # el detalle va en un bloque de código más largo que sus comillas invertidas


def test_code_span():
    assert report_md.code("a`b") == "``a`b``"
    assert report_md.code("`a`") == "`` `a` ``"
    assert report_md.code("a\nb") == "`a b`"
    assert report_md.MARKER not in report_md.code(report_md.MARKER)


def test_committed_example_carries_nothing_reserved():
    """`docs/comentario-pr.md` es salida real con Docker: tampoco puede filtrar el material reservado."""
    r, plan = run()
    doc = (ROOT / "docs" / "comentario-pr.md").read_text()
    assert "### Cachai: APROBADO" in doc and "### Cachai: RECHAZADO" in doc
    # Una marca que también está en el código de la base ya es pública: la ve cualquiera que lea
    # la rama (p. ej. el total 1428.0, que un test de la base afirma y el assert del PR muestra).
    base = "\n".join(f.read_text() for f in (ROOT / "cases" / "facturas" / "base").rglob("*.py"))
    leaked = [s for s in reserved_strings(plan, r) if s in doc and s not in base]
    assert not leaked, leaked
