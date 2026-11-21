"""Parchador: qué recibe el agente y cómo queda su parche."""

import json
import tempfile
from pathlib import Path

import pytest

from cachai import bench, fixer, gitio
from cachai.case import load_case, materialize

FACTURAS = Path(__file__).resolve().parent.parent / "cases" / "facturas"


@pytest.fixture(scope="module")
def case():
    return load_case(FACTURAS)


@pytest.mark.parametrize("policy", sorted(fixer.POLICIES))
def test_brief_shows_visible_attacks_and_nothing_reserved(case, policy):
    split, attacks = case.split(), {a.id: a for a in case.attacks()}
    text = fixer.brief([attacks[i] for i in split.visible], policy)
    for i in split.visible:
        assert attacks[i].request["path"] in text
    visible_paths = {attacks[i].request["path"] for i in split.visible}
    for i in split.reserved:
        path = attacks[i].request["path"]
        # Ninguna ruta reservada aparece, salvo como prefijo de una visible (p. ej. /invoices/1 dentro de /invoices/1/pdf).
        assert path not in text or any(path in v for v in visible_paths), path
    assert "reservad" not in text and "/v2/" not in text and "export" not in text and "total" not in text


def test_policies_differ_only_in_the_instruction(case):
    split, attacks = case.split(), {a.id: a for a in case.attacks()}
    visible = [attacks[i] for i in split.visible]
    normal, minimal = fixer.brief(visible, "normal").splitlines(), fixer.brief(visible, "minimo").splitlines()
    assert [l for l in normal if l.startswith("- ")] == [l for l in minimal if l.startswith("- ")]
    assert normal != minimal


needs_isolation = pytest.mark.skipif(not fixer.isolation_available(), reason="requiere unshare y espacios de nombres de usuario")


@pytest.fixture
def agent_dir():
    """Los agentes falsos van como `python3 -c`: no dependen de ningún archivo que el aislamiento tape."""
    return None


def fake_agent(agent_dir, body: str) -> list[str]:
    return ["python3", "-c", body, "{prompt}", "{model}"]


@pytest.fixture
def trusted(case, tmp_path):
    repo = materialize(case, tmp_path / "repo")
    return repo, gitio.resolve(repo, "main")


def propose(case, trusted, name, agent, isolate=True, out=None):
    repo, base = trusted
    masks = fixer.default_masks(case, out or Path("results/bots"))
    secrets = [case.root / "case.json", case.root / case.attacks_file, case.holdout_tests_path]
    return fixer.propose(repo, base, name, agent, "m1", "arregla", masks, secrets, isolate)


EDIT = (
    "import sys, pathlib\n"
    "p = pathlib.Path('invoices/main.py'); p.write_text(p.read_text() + '\\n# parche\\n')\n"
    "print('hecho:', sys.argv[2])\n"
)


@needs_isolation
def test_propose_commits_the_agents_patch_on_its_own_branch(case, trusted, agent_dir, monkeypatch):
    monkeypatch.setitem(fixer.AGENTS, "falso", fake_agent(agent_dir, EDIT))
    prop = propose(case, trusted, "bot-falso-1", "falso")
    repo, base = trusted
    assert prop.changed == ["invoices/main.py"] and prop.error is None and prop.isolated
    assert "+# parche" in prop.diff and prop.transcript == "hecho: m1"
    branch = gitio.resolve(repo, "bot-falso-1")
    assert gitio.read_blob(repo, branch, "invoices/main.py").endswith(b"# parche\n")
    assert gitio.resolve(repo, "main") == base and gitio.is_ancestor(repo, base, branch)
    assert not gitio.read_blob(repo, base, "invoices/main.py").endswith(b"# parche\n")
    assert not [p for p in Path(tempfile.gettempdir()).glob("cachai-agente-*")]  # la carpeta del agente no queda


@needs_isolation
def test_agent_that_changes_nothing_or_fails_is_reported(case, trusted, agent_dir, monkeypatch):
    monkeypatch.setitem(fixer.AGENTS, "nada", fake_agent(agent_dir, "print('no hice nada')\n"))
    assert "no cambió" in propose(case, trusted, "bot-nada-1", "nada").error
    monkeypatch.setitem(fixer.AGENTS, "roto", fake_agent(agent_dir, "raise SystemExit(3)\n"))
    assert "código 3" in propose(case, trusted, "bot-roto-1", "roto").error
    monkeypatch.setitem(fixer.AGENTS, "ausente", ["comando-que-no-existe-cachai", "{prompt}"])
    assert propose(case, trusted, "bot-ausente-1", "ausente").error


SPY = (
    "import json, os, pathlib, subprocess, sys\n"
    "targets = json.loads(os.environ['CP_SPY_TARGETS'])\n"
    "seen = {}\n"
    "for t in targets:\n"
    "    p = pathlib.Path(t)\n"
    "    try:\n"
    "        seen[t] = p.read_text()[:40] if p.is_file() else sorted(x.name for x in p.iterdir())\n"
    "    except OSError as e:\n"
    "        seen[t] = None\n"
    "git = subprocess.run(['git', 'branch', '-a'], capture_output=True, text=True)\n"
    "print(json.dumps({'seen': seen, 'git': git.stdout + git.stderr, 'here': sorted(str(p) for p in pathlib.Path('.').rglob('*'))}))\n"
)


@needs_isolation
def test_the_agent_cannot_read_reserved_material_or_solutions(case, trusted, agent_dir, tmp_path, monkeypatch):
    """Ni por ruta absoluta, ni en /tmp, ni por git: solo ve los archivos de la base."""
    repo, _ = trusted
    witness = tmp_path / "testigo.txt"
    witness.write_text("no deberías leer esto")
    targets = [
        str(case.root / "case.json"), str(case.root / case.attacks_file), str(case.holdout_tests_path / "test_funcional.py"),
        str(case.root / "prs" / "parche-correcto" / "invoices" / "main.py"), str(witness), str(repo), str(repo / ".git"),
        str(Path(fixer.__file__)),
    ]
    monkeypatch.setenv("CP_SPY_TARGETS", json.dumps(targets))
    monkeypatch.setitem(fixer.AGENTS, "espia", fake_agent(agent_dir, SPY))
    prop = propose(case, trusted, "bot-espia-1", "espia")
    report = json.loads(prop.transcript)
    assert all(v is None or v == [] for v in report["seen"].values()), report["seen"]
    assert "parche-correcto" not in report["git"] and "trampa" not in report["git"]  # no hay repo git en su carpeta
    here = report["here"]
    assert "invoices/main.py" in here and "tests/test_invoices.py" in here
    assert not any(".git" in h or "reservados" in h or "attacks.json" in h or h == "case.json" for h in here)


def test_without_isolation_the_same_probe_does_read_them(case, trusted, agent_dir, monkeypatch):
    """Control del test anterior: sin aislar, el agente sí alcanza el material del host."""
    monkeypatch.setenv("CP_SPY_TARGETS", json.dumps([str(case.root / "case.json")]))
    monkeypatch.setitem(fixer.AGENTS, "espia", fake_agent(agent_dir, SPY))
    prop = propose(case, trusted, "bot-espia-2", "espia", isolate=False)
    assert json.loads(prop.transcript)["seen"][str(case.root / "case.json")] and not prop.isolated


@needs_isolation
def test_isolation_is_verified_before_launching(case, trusted, agent_dir, monkeypatch):
    """Si una carpeta reservada no queda tapada, el agente no llega a correr."""
    monkeypatch.setitem(fixer.AGENTS, "falso", fake_agent(agent_dir, EDIT))
    repo, base = trusted
    with pytest.raises(fixer.IsolationError):
        fixer.propose(repo, base, "bot-falso-2", "falso", "m", "x", masks=[], secrets=[case.root / "case.json"])


@needs_isolation
@pytest.mark.parametrize("body,expected", [
    ("import os\nos.symlink('/etc/hostname', 'enlace')\n", "enlace simbólico"),
    ("import pathlib\npathlib.Path('.git/hooks').mkdir(parents=True)\npathlib.Path('.git/hooks/pre-commit').write_text('x')\n", "metadatos de git"),
])
def test_untrusted_output_is_rejected(case, trusted, agent_dir, monkeypatch, body, expected):
    monkeypatch.setitem(fixer.AGENTS, "malo", fake_agent(agent_dir, body))
    prop = propose(case, trusted, "bot-malo-1", "malo")
    assert expected in prop.error and prop.changed == []


@needs_isolation
def test_deleted_and_added_files_reach_the_branch(case, trusted, agent_dir, monkeypatch):
    monkeypatch.setitem(fixer.AGENTS, "falso", fake_agent(agent_dir, (
        "import pathlib\n"
        "pathlib.Path('tests/test_invoices.py').unlink()\n"
        "pathlib.Path('invoices/nuevo.py').write_text('x = 1\\n')\n")))
    prop = propose(case, trusted, "bot-falso-3", "falso")
    assert sorted(prop.changed) == ["invoices/nuevo.py", "tests/test_invoices.py"]


@needs_isolation
@pytest.mark.parametrize("body", [
    # Una carpeta de la base pasa a ser un archivo.
    "import shutil, pathlib\nshutil.rmtree('invoices')\npathlib.Path('invoices').write_text('ya no es carpeta')\n",
    # Un archivo de la base pasa a ser una carpeta.
    "import pathlib\npathlib.Path('pytest.ini').unlink()\npathlib.Path('pytest.ini').mkdir()\npathlib.Path('pytest.ini/x.txt').write_text('x')\n",
])
def test_file_and_directory_swaps_are_applied_without_touching_the_base(case, trusted, agent_dir, monkeypatch, body):
    repo, base = trusted
    monkeypatch.setitem(fixer.AGENTS, "cambia", fake_agent(agent_dir, body))
    prop = propose(case, trusted, "bot-cambia-1", "cambia")
    assert prop.error is None and prop.changed
    branch = gitio.resolve(repo, "bot-cambia-1")
    paths = {path for _, _, _, path in gitio.ls_tree(repo, branch)}
    assert ("invoices" in paths) != ("pytest.ini/x.txt" in paths)
    # La base queda igual y el repo confiable, limpio y en main.
    assert gitio.resolve(repo, "main") == base
    assert {p for _, _, _, p in gitio.ls_tree(repo, base)} >= {"invoices/main.py", "pytest.ini"}
    assert (repo / "invoices" / "main.py").is_file() and (repo / "pytest.ini").is_file()
    import subprocess
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout == ""


@needs_isolation
def test_attempt_whose_review_cannot_run_is_still_recorded(case, tmp_path, agent_dir, monkeypatch):
    """Si la revisión del parche revienta, el intento queda guardado con su causa."""
    monkeypatch.setitem(fixer.AGENTS, "falso", fake_agent(agent_dir, EDIT))
    monkeypatch.setitem(fixer.DEFAULT_MODELS, "falso", "m")

    def boom(*a, **k):
        raise RuntimeError("docker no responde")

    monkeypatch.setattr(fixer.pipeline, "check", boom)
    rows = fixer.run(case, "falso", runs=2, out_dir=tmp_path / "out")
    assert len(rows) == 2 and all("docker no responde" in r["error"] and r["approved"] == {} for r in rows)
    assert all(r["changed"] == ["invoices/main.py"] for r in rows)
    (s,) = bench.summarize_bots(rows)
    assert (s["without_patch"], s["incomplete"]) == (0, 2)
    for r in rows:
        folder = tmp_path / "out" / case.id / r["name"]
        assert "+# parche" in (folder / "parche.diff").read_text() and (folder / "pedido.md").is_file()


@needs_isolation
def test_workdir_under_a_masked_folder_is_still_usable(case, trusted, agent_dir, tmp_path, monkeypatch):
    """La carpeta del agente vive en /tmp, que queda tapado: se vuelve a montar solo ella."""
    monkeypatch.setitem(fixer.AGENTS, "mira", fake_agent(agent_dir, (
        "import os, pathlib, tempfile\n"
        "here = pathlib.Path.cwd()\n"
        "others = [p.name for p in pathlib.Path(tempfile.gettempdir()).iterdir() if p not in here.parents and p != here]\n"
        "pathlib.Path('nota.txt').write_text('ok')\n"
        "print(str(here).startswith(tempfile.gettempdir()), others)\n")))
    prop = propose(case, trusted, "bot-mira-1", "mira")
    assert prop.transcript == "True []" and prop.changed == ["nota.txt"]


def test_summary_never_replaces_an_attempt(case, tmp_path):
    row = {"name": "bot-a-1", "approved": {}}
    fixer.write_summary(case, [row], tmp_path)
    fixer.write_summary(case, [{"name": "bot-a-2", "approved": {}}], tmp_path)
    with pytest.raises(ValueError):
        fixer.write_summary(case, [{"name": "bot-a-1", "approved": {"full": True}}], tmp_path)
    saved = json.loads((tmp_path / case.id / "bots.json").read_text())
    assert [r["name"] for r in saved] == ["bot-a-1", "bot-a-2"] and saved[0]["approved"] == {}


@needs_isolation
def test_two_runs_with_the_same_settings_keep_both(case, tmp_path, agent_dir, monkeypatch):
    """Repetir la misma configuración no pisa los intentos anteriores (aquí el agente no produce parche)."""
    monkeypatch.setitem(fixer.AGENTS, "nada", fake_agent(agent_dir, "print('nada')\n"))
    monkeypatch.setitem(fixer.DEFAULT_MODELS, "nada", "m")
    out = tmp_path / "out"
    first = fixer.run(case, "nada", runs=1, out_dir=out)
    fixer.write_summary(case, first, out)
    second = fixer.run(case, "nada", runs=1, out_dir=out)
    fixer.write_summary(case, second, out)
    assert first[0]["name"] != second[0]["name"] and first[0]["experiment"] != second[0]["experiment"]
    saved = json.loads((out / case.id / "bots.json").read_text())
    assert len(saved) == 2 and all(r["error"] and r["approved"] == {} and r["isolated"] for r in saved)
    for r in saved:
        assert (out / case.id / r["name"] / "respuesta.txt").read_text() == "nada\n"


def test_refuses_to_run_without_isolation_unless_asked(case, monkeypatch):
    monkeypatch.setattr(fixer, "isolation_available", lambda: False)
    with pytest.raises(fixer.IsolationError):
        fixer.run(case, "claude", runs=1)


def test_unknown_agent_is_rejected(case):
    with pytest.raises(ValueError):
        fixer.run(case, "magia")


def test_bot_summary_counts_passed_own_but_rejected():
    bots = [
        {"agent": "a", "model": "m", "policy": "normal", "isolated": True, "approved": {"ci": True, "visible": True, "full": True}},
        {"agent": "a", "model": "m", "policy": "minimo", "approved": {"ci": True, "visible": True, "full": False}},
        {"agent": "a", "model": "m", "policy": "minimo", "approved": {"ci": True, "visible": False, "full": False}},
        {"agent": "a", "model": "m", "policy": "minimo", "approved": {}},
    ]
    normal, minimal = bench.summarize_bots(bots)
    assert (normal["n"], normal["full_approved"], normal["passed_own_but_rejected"], normal["isolated"]) == (1, 1, 0, True)
    assert not minimal["isolated"]  # filas sin la marca de aislamiento no se dan por aisladas
    assert (minimal["n"], minimal["without_patch"], minimal["visible_approved"], minimal["passed_own_but_rejected"]) == (3, 1, 1, 1)
    assert any("Política de ataque" in line for line in bench.bots_markdown(bots))


def test_patch_whose_review_crashed_counts_as_a_patch_without_verdict():
    """Hubo parche; lo que falló fue la revisión. No es un intento sin parche ni un rechazo."""
    bots = [
        {"agent": "a", "model": "m", "policy": "normal", "isolated": True, "changed": ["invoices/main.py"], "approved": {},
         "outcome": {}, "error": "la revisión del parche no se pudo correr: RuntimeError: docker no responde"},
        {"agent": "a", "model": "m", "policy": "normal", "isolated": True, "changed": [], "approved": {}, "outcome": {},
         "error": "el agente no cambió ningún archivo"},
    ]
    (s,) = bench.summarize_bots(bots)
    assert (s["n"], s["without_patch"], s["incomplete"], s["passed_own_but_rejected"], s["full_approved"], s["visible_approved"]) == (2, 1, 1, 0, 0, 0)
    row = [l for l in bench.bots_markdown(bots) if l.startswith("| Encargo normal")][0]
    assert row.endswith("| 2 | 1 | 0 | 0 | 0 | 1 | sí |")


def test_bot_review_without_verdict_is_not_a_rejection():
    bots = [{"agent": "a", "model": "m", "policy": "normal", "isolated": True,
             "approved": {"ci": True, "visible": True, "full": False}, "outcome": {"ci": "approved", "visible": "approved", "full": "incomplete"}}]
    (s,) = bench.summarize_bots(bots)
    assert (s["passed_own_but_rejected"], s["incomplete"], s["full_approved"]) == (0, 1, 0)
    row = [l for l in bench.bots_markdown(bots) if l.startswith("| Encargo normal")][0]
    assert row.endswith("| 1 | 0 | 0 | 1 | sí |")
