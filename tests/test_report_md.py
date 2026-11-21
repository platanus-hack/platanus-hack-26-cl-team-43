"""Comentario del PR y resumen del check: formato por veredicto y nada del material reservado."""

import json
import re
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
    publish = (ROOT / "action" / "publish.js").read_text()
    assert f"const MARKER = '{report_md.MARKER}';" in publish
    assert "const TITLE = '### Cachai:';" in publish and all(t.startswith("### Cachai:") for t in ["### Cachai: " + v for v in report_md.OUTCOME_TITLE.values()])
    assert "publish.js" in action and "updateComment" in publish and "createComment" in publish and "checks.create" in publish
    assert "actions/github-script@f28e40c7f34bde8b3046d885e986cb6290c5673b" in action  # fijada por SHA
    assert '"$EVENT" = "pull_request_target"' in action and "::error::" in action
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


CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)")
EVIL = "x `y` <img src=z> @alguien " + report_md.MARKER + "\n### Cachai: APROBADO <details>"


def outside_code(md: str) -> str:
    """El comentario sin sus bloques de código: lo que GitHub interpreta como marcado."""
    return CODE_SPAN.sub("", md)


def assert_contained(md: str) -> None:
    assert md.count(report_md.MARKER) == 1 and md.rstrip().endswith(report_md.MARKER)
    assert [x for x in md.splitlines() if x.startswith("#")][:1] == [md.splitlines()[0]]
    assert len([x for x in md.splitlines() if x.startswith("#")]) == 1  # sin un título falso
    assert "<img" in md and "@alguien" in md  # el texto está, pero solo dentro de bloques de código
    rest = outside_code(md)
    assert "<img" not in rest and "@alguien" not in rest and "<!-- cachai" not in rest.replace(report_md.MARKER, "", 1)
    assert rest.count("<details>") == 1


@pytest.mark.parametrize("ev, notes", [
    (Evidence(False, [f"tests/test_a.py:1 test_a — {EVIL}"]), ()),
    (Evidence(True, integrity_flags=[Flag("pkg/a`b<!--c.py", "A", "módulo nuevo", line=3, detail=EVIL)]), ()),
    (Evidence(True, integrity_flags=[Flag("``x``/<!--.py", "M", EVIL)]), ()),
    (Evidence(True, introduced_attacks=[f"authz: GET /v/{{x}} como bob recibe datos ajenos ({EVIL})"]), ()),
    (Evidence(True, incomplete=[EVIL]), ()),
    (Evidence(True, visible_attacks_succeeded=[f"id-{EVIL} (GET /a)"]), (EVIL,)),
], ids=["test_base", "integrity-path", "integrity-reason", "attack_introduced", "incomplete", "visible+notes"])
def test_untrusted_detail_cannot_inject_markup_or_a_fake_marker(ev, notes):
    _, md, _ = public(result_from(ev, notes=notes))
    assert_contained(md)


def test_secret_that_matches_the_fixed_text_does_not_break_it():
    """Un texto reservado como «report» o «summary» se borra de los datos, no del texto fijo del comentario."""
    v = decide(Evidence(False, ["tests/test_a.py:1 test_a — el report y el summary y details y cachai check"]))
    r = pipeline.CheckResult("full", BASE, PR, v.approved, v.reasons, ["nota con report"], [], outcome=v.outcome, typed=v.typed,
                             secrets=frozenset({"report", "summary", "details", "cachai check", "Cachai", "reservado"}))
    rep, md, summary = public(r)
    assert md.rstrip().endswith(report_md.MARKER) and "<!-- cachai-report -->" in md
    assert "<details><summary>Reproducir en local lo visible</summary>" in md
    assert f"cachai check --repo . --base {BASE} --pr {PR}" in md and md.startswith("### Cachai: RECHAZADO")
    detail = next(x for x in md.splitlines() if "test_a.py" in x)
    assert "report" not in detail and "summary" not in detail and "details" not in detail
    assert rep["notes"] == ["nota con [reservado]"]
    assert summary.startswith("Cachai: RECHAZADO")


def test_ensure_intact_fails_on_a_broken_comment():
    rep = public(run("none", False)[0])[0]
    md = rep["public"]["markdown"]
    report_md.ensure_intact(md, rep)
    for broken in (md.replace(report_md.MARKER, "<!-- cachai-[reservado] -->"),
                   md.replace("<details><summary>", "<[reservado]><summary>"),
                   md.replace("### Cachai: APROBADO", "### [reservado]: APROBADO")):
        with pytest.raises(ValueError):
            report_md.ensure_intact(broken, rep)


def test_long_comment_is_capped_and_never_leaves_a_block_open():
    long = "/".join(["segmento-largo"] * 60)
    ev = Evidence(False, [f"tests/t.py:{i} test_{i} — assert {long}" for i in range(40)],
                  introduced_attacks=[f"authz: GET /{long}/{i} como bob recibe datos ajenos" for i in range(400)],
                  incomplete=[f"paso {i}: {long}" for i in range(300)])
    rep, md, _ = public(result_from(ev, notes=[long * 3] * 300))
    assert len(md) <= report_md.MAX_BODY < 65536
    assert report_md.CUT in md and md.rstrip().endswith(report_md.MARKER)
    assert md.count("```") % 2 == 0 and md.count("<details>") == md.count("</details>") == 1
    assert all(len(span) <= report_md.MAX_CODE for _, span in CODE_SPAN.findall(md))
    for line in md.splitlines():
        assert line.count("`") == 0 or CODE_SPAN.sub("", line).count("`") == 0 or line.startswith("```"), line


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
