"""La página pública se arma solo con lo que hay en results/."""

import importlib.util
import json
import shutil
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts" / "build_site.py")
build_site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_site)


def test_page_is_built_from_saved_results():
    page = build_site.build(ROOT / "results")
    assert "{{" not in page and "<h1>Ninguna IA aprueba el código de otra IA.</h1>" in page
    parser = Scripts()
    parser.feed(page)
    data = json.loads(dict(parser.scripts)["data"])
    bench = json.loads((ROOT / "results" / "bench.json").read_text())
    assert data["summary"] == bench["summary"] and len(data["runs"]) == len(bench["runs"])
    assert [d["pr"] for d in data["demo"]] == ["trampa-paquete", "parche-a-medias", "parche-correcto"]
    prs = len({r["pr"] for r in bench["runs"]})
    assert f"Medición sobre {prs} cambios etiquetados" in page
    full = {r["pr"]: r for r in data["runs"] if r["mode"] == "full"}
    assert full["parche-correcto"]["checked"] and not full["parche-a-medias"]["checked"]
    assert any("reservados" in line for line in full["parche-correcto"]["checked"])
    assert all(b["status"] in ("aprobado", "rechazado") for b in data["bots"]["runs"])


class Scripts(HTMLParser):
    """Extrae el contenido de cada <script> como lo ve un parser de HTML."""

    def __init__(self):
        super().__init__()
        self.scripts, self._open = [], None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._open = [dict(attrs).get("id"), ""]

    def handle_data(self, data):
        if self._open is not None:
            self._open[1] += data

    def handle_endtag(self, tag):
        if tag == "script" and self._open is not None:
            self.scripts.append(tuple(self._open))
            self._open = None


def results_copy(tmp_path) -> Path:
    dst = tmp_path / "results"
    shutil.copytree(ROOT / "results", dst)
    return dst


def edit(results: Path, fn) -> None:
    f = results / "bench.json"
    data = json.loads(f.read_text())
    fn(data)
    f.write_text(json.dumps(data, ensure_ascii=False))


@pytest.mark.parametrize("nasty", [
    "</script><script>alert(1)</script>",
    "<!--<script>",
    "<!-- <script> --> </script>",
    "a & b < c > d \u2028 e",
])
def test_reasons_cannot_break_the_embedded_data(tmp_path, nasty):
    """Los motivos traen texto que devolvió la app: no debe poder alterar la página."""
    results = results_copy(tmp_path)

    def poison(data):
        run = next(r for r in data["runs"] if r["pr"] == "trampa-paquete" and r["mode"] == "full")
        run["reasons"].append(nasty)
        report = results / "runs" / run["case"] / run["pr"] / "full.json"
        rep = json.loads(report.read_text())
        rep["reasons"].append(nasty)
        report.write_text(json.dumps(rep, ensure_ascii=False))

    edit(results, poison)
    page = build_site.build(results)
    parser = Scripts()
    parser.feed(page)
    assert [name for name, _ in parser.scripts] == ["data", None]
    payload = dict(parser.scripts)["data"]
    assert "<" not in payload and ">" not in payload and "&" not in payload
    run = next(r for r in json.loads(payload)["runs"] if r["pr"] == "trampa-paquete" and r["mode"] == "full")
    assert run["reasons"][-1] == nasty


def test_bench_and_report_must_agree(tmp_path):
    results = results_copy(tmp_path)
    report = results / "runs" / "facturas" / "parche-correcto" / "full.json"
    rep = json.loads(report.read_text())
    rep.update(approved=False, outcome="rejected", reasons=["ataque reservado sigue funcionando: x"])
    report.write_text(json.dumps(rep))
    with pytest.raises(ValueError, match="no coincide"):
        build_site.build(results)


def test_approved_report_with_a_confirmed_attack_is_not_published(tmp_path):
    results = results_copy(tmp_path)
    report = results / "runs" / "facturas" / "parche-correcto" / "full.json"
    rep = json.loads(report.read_text())
    rep["evaluator"]["attacks"]["results"][0]["outcome"] = "confirmed"
    report.write_text(json.dumps(rep))
    with pytest.raises(ValueError, match="contradictorio"):
        build_site.build(results)


def bot(policy="normal", changed=("a.py",), visible=True, full=True, outcome=None, isolated=True):
    approved = {} if full is None else {"ci": True, "visible": visible, "full": full}
    out = {} if full is None else {"full": outcome or ("approved" if full else "rejected")}
    return {"name": f"bot-{policy}", "case": "facturas", "agent": "a", "model": "m", "policy": policy, "isolated": isolated,
            "changed": list(changed), "approved": approved, "outcome": out}


def conclusions(bots):
    from cachai import bench
    return build_site.bot_conclusions(bench.summarize_bots(bots))


def test_model_conclusions_follow_the_results():
    assert conclusions([bot(), bot()]) == ["Con el encargo normal hubo 2 intentos y 2 parches. Cachai aprobó 2 de 2."]
    # Un parche normal rechazado: la frase no puede seguir diciendo que aprobó todos.
    (line,) = conclusions([bot(), bot(full=False)])
    assert "aprobó 1 de 2" in line and "1 pasó su propia validación y se rechazó con evidencia" in line
    # Un intento sin parche y un parche sin revisión no se confunden.
    (line,) = conclusions([bot(), bot(changed=(), full=None), bot(full=None)])
    assert "3 intentos y 2 parches" in line and "aprobó 1 de 2" in line and "1 quedó sin veredicto" in line
    (line,) = conclusions([bot(isolated=False)])
    assert "No todos los intentos corrieron con el agente aislado" in line
    assert len(conclusions([bot(), bot("minimo", full=False)])) == 2


def test_isolation_sentence_follows_the_data(tmp_path):
    assert "Todos los intentos corrieron con el agente aislado" in build_site.build(ROOT / "results")
    results = results_copy(tmp_path)

    def one_not_isolated(data):
        pass

    f = results / "bots" / "facturas" / "bots.json"
    bots = json.loads(f.read_text())
    bots[0]["isolated"] = False
    f.write_text(json.dumps(bots))

    def sync(data):
        from cachai import bench
        loaded = bench.load_bots(results)
        data["bots"] = {"summary": bench.summarize_bots(loaded), "runs": loaded}

    edit(results, sync)
    page = build_site.build(results)
    assert "Todos los intentos corrieron con el agente aislado" not in page
    assert "1 intento corrió sin aislar al agente" in page and "No todos los intentos corrieron con el agente aislado" in page


def test_attempt_status_matches_the_bench_classification():
    assert build_site.attempt_status(bot()) == "aprobado"
    assert build_site.attempt_status(bot(full=False)) == "rechazado"
    assert build_site.attempt_status(bot(full=False, outcome="incomplete")) == "sin veredicto"
    assert build_site.attempt_status(bot(changed=(), full=None)) == "sin parche"
    assert build_site.attempt_status(bot(full=None)) == "sin veredicto"


def test_every_verdict_names_its_reviewer_for_assistive_tech():
    page = build_site.build(ROOT / "results")
    assert 'class: "sr", text: WHO[m] + ": "' in page and ".sr {" in page


def test_committed_page_matches_the_generator():
    """site/index.html es el resultado de correr el generador sobre results/."""
    assert (ROOT / "site" / "index.html").read_text() == build_site.build(ROOT / "results")
