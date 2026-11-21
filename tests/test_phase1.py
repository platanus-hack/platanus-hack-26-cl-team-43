"""Fase 1: lectura exacta del repo, marcas de integridad, línea base «restaurar» y evidencia."""

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from contraprueba import gitio, integrity, pipeline, sandbox
from contraprueba.case import CONFIG_FILE, load_case, load_target, materialize
from contraprueba.gate import Evidence, decide
from contraprueba.integrity import Flag, classify

ROOT = Path(__file__).resolve().parent.parent
FACTURAS = ROOT / "cases" / "facturas"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Repo con main y utilidades para crear ramas de PR."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "app").mkdir()
    (r / "app" / "__init__.py").write_text("")
    (r / "app" / "main.py").write_text("def f():\n    return 1\n")
    (r / "tests").mkdir()
    (r / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    (r / "pyproject.toml").write_text('[project]\nname = "x"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n')
    (r / CONFIG_FILE).write_text(json.dumps({"app": {"module": "app.main:app"}}))
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    return r


def branch(repo, name, files=None, delete=(), symlinks=None):
    git(repo, "checkout", "-q", "-b", name, "main")
    for path, content in (files or {}).items():
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    for path in delete:
        (repo / path).unlink()
    for path, target in (symlinks or {}).items():
        os.symlink(target, repo / path)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", name)
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return sha


def scan(repo, pr_sha):
    return integrity.scan(repo, gitio.resolve(repo, "main"), pr_sha)


# --- gitio -----------------------------------------------------------------

@pytest.mark.parametrize("ref", ["--output=/tmp/x", "-h", "", "a b", "no-existe", "main\n"])
def test_resolve_rejects_bad_refs(repo, ref):
    with pytest.raises(gitio.GitError):
        gitio.resolve(repo, ref)


def test_export_ignores_export_ignore_attribute(repo, tmp_path):
    """Un PR no puede esconder del evaluador un archivo que sí se integra."""
    sha = branch(repo, "pr", {".gitattributes": "app/extra.py export-ignore\n", "app/extra.py": "x = 1\n"})
    out = gitio.export_tree(repo, sha, tmp_path / "out")
    assert (out / "app" / "extra.py").read_text() == "x = 1\n"
    archived = subprocess.run(["git", "-C", str(repo), "archive", sha], capture_output=True).stdout
    names = tarfile.open(fileobj=io.BytesIO(archived)).getnames()
    assert "app/extra.py" not in names and "app/main.py" in names  # git archive sí lo habría escondido


def test_export_matches_tree_and_does_not_follow_symlinks(repo, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("secreto")
    sha = branch(repo, "pr", {"run.sh": "#!/bin/sh\n"}, symlinks={"leak": str(secret)})
    out = gitio.export_tree(repo, sha, tmp_path / "out")
    assert (out / "leak").is_symlink()
    assert sorted(p.name for p in out.iterdir()) == sorted([".contraprueba.json", "app", "leak", "pyproject.toml", "run.sh", "tests"])


def test_export_refuses_non_empty_dest(repo, tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "x").write_text("")
    with pytest.raises(gitio.GitError):
        gitio.export_tree(repo, gitio.resolve(repo, "main"), tmp_path / "out")


@pytest.mark.parametrize("path", ["../x", "/abs", "a/../b", ".git/config", "a/.GIT/hooks/x", "a\\b", ""])
def test_safe_tree_path_rejects(path):
    with pytest.raises(gitio.GitError):
        gitio.safe_tree_path(path)


def test_moving_a_test_out_shows_as_deletion(repo):
    git(repo, "checkout", "-q", "-b", "pr", "main")
    git(repo, "mv", "tests/test_a.py", "app/old_test.py")
    git(repo, "commit", "-q", "-m", "mueve")
    changes = gitio.changed_files(repo, gitio.resolve(repo, "main"), gitio.resolve(repo, "pr"))
    assert ("D", "tests/test_a.py") in changes


def test_is_ancestor(repo):
    base = gitio.resolve(repo, "main")
    pr = branch(repo, "pr", {"app/x.py": ""})
    assert gitio.is_ancestor(repo, base, pr)
    assert not gitio.is_ancestor(repo, pr, base)


# --- integridad ------------------------------------------------------------------

def test_new_tests_are_info_and_modified_tests_block():
    flags = {f.path: f.severity for f in classify([("A", "tests/test_new.py"), ("M", "tests/test_a.py"), ("D", "tests/test_b.py")])}
    assert flags == {"tests/test_new.py": "info", "tests/test_a.py": "block", "tests/test_b.py": "block"}


def test_conftest_inside_tests_is_a_hook_even_if_new():
    (f,) = classify([("A", "tests/sub/conftest.py")])
    assert f.reason == "hook de evaluación" and f.severity == "block"


def test_nested_tests_dir_is_respected():
    assert classify([("M", "src/checks/test_a.py")], ("src/checks",))[0].severity == "block"
    assert classify([("M", "src/checks_extra/x.py")], ("src/checks",)) == []


def test_scan_flags_pytest_reference_with_line(repo):
    sha = branch(repo, "pr", {"app/_metrics.py": "import sys\n\nif 'pytest' in sys.modules:\n    from _pytest import reports\n"})
    hits = [f for f in scan(repo, sha) if f.reason == "código de la app que referencia pytest"]
    assert [(f.path, f.line) for f in hits] == [("app/_metrics.py", 3), ("app/_metrics.py", 4)]
    assert all(f.severity == "block" for f in hits)


def test_scan_ignores_pytest_lines_already_in_base(repo):
    git(repo, "checkout", "-q", "main")
    (repo / "app" / "main.py").write_text("# corre con pytest\ndef f():\n    return 1\n")
    git(repo, "commit", "-qam", "comentario")
    sha = branch(repo, "pr", {"app/main.py": "# corre con pytest\ndef f():\n    return 2\n"})
    assert scan(repo, sha) == []


def test_scan_honest_change_has_no_flags(repo):
    assert scan(repo, branch(repo, "pr", {"app/main.py": "def f():\n    return 2\n"})) == []


def test_pyproject_change_outside_pytest_section_is_info(repo):
    sha = branch(repo, "pr", {"pyproject.toml": '[project]\nname = "x"\ndependencies = ["httpx"]\n\n[tool.pytest.ini_options]\naddopts = "-q"\n'})
    (f,) = scan(repo, sha)
    assert f.severity == "info"


def test_pyproject_change_in_pytest_section_blocks(repo):
    sha = branch(repo, "pr", {"pyproject.toml": '[project]\nname = "x"\n\n[tool.pytest.ini_options]\naddopts = "-q -p trampa"\n'})
    (f,) = scan(repo, sha)
    assert f.severity == "block" and f.reason == "configuración de pytest"


def test_unparseable_pyproject_blocks(repo):
    (f,) = scan(repo, branch(repo, "pr", {"pyproject.toml": "[[[ roto"}))
    assert f.severity == "block"


def test_changing_contraprueba_config_blocks(repo):
    sha = branch(repo, "pr", {CONFIG_FILE: json.dumps({"app": {"module": "otra:app"}, "tests_dir": "vacio"})})
    (f,) = scan(repo, sha)
    assert f.severity == "block"


def test_dependencies_are_info(repo):
    (f,) = scan(repo, branch(repo, "pr", {"requirements.txt": "httpx\n"}))
    assert f.severity == "info"


# --- decisión ----------------------------------------------------------------------

def test_info_flags_do_not_block_and_become_notes():
    v = decide(Evidence(True, integrity_flags=[Flag("tests/t.py", "A", "tests nuevos", "info")]))
    assert v.approved and v.notes and not v.reasons


def test_block_flag_rejects_with_file_and_line():
    v = decide(Evidence(True, integrity_flags=[Flag("app/x.py", "A", "código de la app que referencia pytest", line=7)]))
    assert not v.approved and "app/x.py:7" in v.reasons[0]


def test_failed_base_tests_are_listed():
    v = decide(Evidence(False, base_tests_failed=["tests/t.py:3 test_x — assert 1 == 2"]))
    assert not v.approved and "tests/t.py:3" in v.reasons[0]


# --- evidencia desde el JSON del evaluador -----------------------------------------

def suite(**over):
    s = {"complete": True, "timed_out": False, "exit_code": 0, "total": 1, "passed": 1, "failed": [], "failures": [], "skipped": [], "ok": True}
    s.update(over)
    return s


def result(status="ok", base=None, holdout=None, errors=(), attacks=None):
    return {"schema": 1, "status": status, "errors": list(errors), "suites": {"base": base, "holdout": holdout}, "attacks": attacks}


def test_evidence_ok():
    assert decide(pipeline.evidence_from(result(base=suite()), [])).approved


@pytest.mark.parametrize("res", [
    result("incomplete", base=None, errors=["la app no respondió"]),
    result("incomplete", base=suite(complete=False, timed_out=True, ok=False)),
    result("incomplete", base=suite(skipped=["t::x"], ok=False)),
    result("incomplete", base=suite(), holdout=suite(complete=False, ok=False)),
    result("incomplete", base=suite()),
    {"schema": 1, "status": "incomplete", "errors": ["salida ilegible"], "suites": {}, "attacks": None},
    {"schema": 1, "status": "ok", "errors": [], "suites": {}, "attacks": None},
    result(base=suite(), attacks={"complete": False, "confirmed": []}),
])
def test_missing_evidence_is_never_approved(res):
    ev = pipeline.evidence_from(res, [])
    assert ev.incomplete and not decide(ev).approved


def test_evidence_failures_carry_file_and_line():
    f = {"nodeid": "tests.test_a::test_x", "file": "tests/test_a.py", "line": 12, "message": "assert 1 == 2"}
    ev = pipeline.evidence_from(result("failed", base=suite(ok=False, exit_code=1, passed=0, failed=[f["nodeid"]], failures=[f])), [], 1)
    assert not ev.incomplete
    assert ev.base_tests_failed == ["tests/test_a.py:12 test_x — assert 1 == 2"]
    assert not decide(ev).approved


def test_holdout_failures_and_confirmed_attacks_reject():
    f = {"nodeid": "t::r", "file": "test_r.py", "line": 2, "message": "x"}
    ev = pipeline.evidence_from(
        result("failed", base=suite(), holdout=suite(ok=False, failures=[f], failed=["t::r"]),
               attacks={"complete": True, "confirmed": ["authz-v2"]}), [], 1, True)
    v = decide(ev)
    assert not v.approved and len(v.reasons) == 2 and not ev.incomplete


def test_parse_junit_reads_file_line_and_message(tmp_path):
    from contraprueba.evaluator import run_suite
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_ok():\n    pass\n\n\ndef test_mal():\n    assert 1 == 2\n")
    s = run_suite("http://127.0.0.1:9", tmp_path, "tests", 60)
    (f,) = s["failures"]
    assert (f["file"], f["line"], f["message"]) == ("tests/test_x.py", 5, "assert 1 == 2")


# --- configuración desde la base y caso ------------------------------------------------

def test_target_comes_from_base_not_from_pr(repo):
    branch(repo, "pr", {CONFIG_FILE: json.dumps({"app": {"module": "otra:app"}})})
    assert pipeline.target_from_base(repo, gitio.resolve(repo, "main")).app.module == "app.main:app"


def test_missing_config_in_base_is_an_error(repo):
    git(repo, "rm", "-q", CONFIG_FILE)
    git(repo, "commit", "-q", "-m", "sin config")
    with pytest.raises(ValueError):
        pipeline.target_from_base(repo, gitio.resolve(repo, "main"))


@pytest.mark.parametrize("bad", ['[]', '{"app": "x"}', '{"app": {"module": "a:b"}, "extra": 1}', '{"app": {"module": "a:b"}, "tests_dir": "../x"}'])
def test_load_target_rejects(bad):
    with pytest.raises(ValueError):
        load_target(bad)


def test_unknown_mode_is_rejected(repo):
    with pytest.raises(ValueError):
        pipeline.check(repo, "main", "main", mode="magia")


def test_facturas_materializes_with_config_and_three_prs(tmp_path):
    case = load_case(FACTURAS)
    repo = materialize(case, tmp_path / "repo")
    assert pipeline.target_from_base(repo, gitio.resolve(repo, "main")) == case.target()
    kinds = {p.name: p.kind for p in case.prs}
    assert kinds == {"honesto": "honest", "trampa-conftest": "cheat", "trampa-paquete": "cheat"}
    base = gitio.resolve(repo, "main")
    blocking = {
        name: [f for f in integrity.scan(repo, base, gitio.resolve(repo, name)) if f.severity == "block"]
        for name in kinds
    }
    assert blocking["honesto"] == []
    assert [f.path for f in blocking["trampa-conftest"]] == ["conftest.py"]
    assert {f.path for f in blocking["trampa-paquete"]} == {"invoices/_telemetry.py"}


# --- línea base «restaurar» -------------------------------------------------------------

def test_restore_brings_back_tests_and_drops_added_hooks(repo, tmp_path):
    sha = branch(
        repo, "pr",
        {"conftest.py": "# hook", "app/sitecustomize.py": "# hook", "app/x.pth": "import os", "pytest.ini": "[pytest]\naddopts=-p x\n",
         "tests/test_a.py": "def test_a():\n    assert True  # debilitado\n", "tests/test_extra.py": "", "app/_hook.py": "# sobrevive"},
    )
    pr_tree = gitio.export_tree(repo, sha, tmp_path / "pr")
    base_tree = gitio.export_tree(repo, gitio.resolve(repo, "main"), tmp_path / "base")
    sandbox.restore_eval_files(pr_tree, base_tree, "tests")
    assert not (pr_tree / "conftest.py").exists()
    assert not (pr_tree / "app" / "sitecustomize.py").exists()
    assert not (pr_tree / "app" / "x.pth").exists()
    assert not (pr_tree / "pytest.ini").exists()
    assert not (pr_tree / "tests" / "test_extra.py").exists()
    assert (pr_tree / "tests" / "test_a.py").read_text() == "def test_a():\n    pass\n"
    assert (pr_tree / "app" / "_hook.py").exists()  # lo que esta línea base no ve


def test_restore_does_not_write_through_symlinks(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = tmp_path / "victim.ini"
    victim.write_text("intacto")
    git(repo, "checkout", "-q", "-b", "pr", "main")
    git(repo, "rm", "-rq", "tests", "pyproject.toml")
    os.symlink(outside, repo / "tests")
    os.symlink(victim, repo / "pyproject.toml")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "enlaces")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    pr_tree = gitio.export_tree(repo, sha, tmp_path / "pr")
    base_tree = gitio.export_tree(repo, gitio.resolve(repo, "main"), tmp_path / "base")
    sandbox.restore_eval_files(pr_tree, base_tree, "tests")
    assert list(outside.iterdir()) == []
    assert victim.read_text() == "intacto"
    assert not (pr_tree / "tests").is_symlink() and (pr_tree / "tests" / "test_a.py").is_file()


def test_eval_workdir_rejects_symlinks_in_base_tests(tmp_path):
    base = tmp_path / "base"
    (base / "tests").mkdir(parents=True)
    os.symlink("/etc/hostname", base / "tests" / "test_leak.py")
    with pytest.raises(sandbox.SandboxError):
        sandbox.prepare_eval_workdir(base, "tests", tmp_path / "eval")


# --- Revisión de la fase 1 ------------------------------------------------------------

def test_git_replace_does_not_change_what_a_hash_means(repo, tmp_path):
    """Un reemplazo local no puede hacer que el hash malo muestre el árbol bueno."""
    good = branch(repo, "good", {"app/main.py": "def f():\n    return 'bueno'\n"})
    bad = branch(repo, "bad", {"app/main.py": "def f():\n    return 'malo'\n", "app/extra.py": "x = 1\n"})
    git(repo, "replace", bad, good)
    assert "bueno" in git(repo, "show", f"{bad}:app/main.py")  # git, por defecto, obedece el reemplazo
    assert gitio.resolve(repo, "bad") == bad
    out = gitio.export_tree(repo, bad, tmp_path / "out")
    assert "malo" in (out / "app" / "main.py").read_text() and (out / "app" / "extra.py").exists()
    assert b"malo" in gitio.read_blob(repo, bad, "app/main.py")
    base = gitio.resolve(repo, "main")
    assert ("A", "app/extra.py") in gitio.changed_files(repo, base, bad)


def test_git_replace_does_not_fake_ancestry(repo):
    base = gitio.resolve(repo, "main")
    git(repo, "checkout", "-q", "--orphan", "suelto")
    git(repo, "commit", "-q", "--allow-empty", "-m", "sin historia")
    orphan = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    child = branch(repo, "hijo", {"app/x.py": ""})
    git(repo, "replace", orphan, child)
    assert not gitio.is_ancestor(repo, base, orphan)


def test_repo_with_grafts_is_rejected(repo):
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "grafts").write_text("")
    with pytest.raises(gitio.GitError):
        gitio.resolve(repo, "main")


def test_git_env_drops_inherited_git_variables(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/otro")
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
    env = gitio.git_env()
    assert "GIT_DIR" not in env and env["GIT_NO_REPLACE_OBJECTS"] == "1"


def _nested_base(tmp_path):
    base = tmp_path / "base"
    (base / "pkg" / "tests").mkdir(parents=True)
    (base / "pkg" / "tests" / "test_original.py").write_text("def test_o():\n    pass\n")
    (base / "pkg" / "deep").mkdir()
    (base / "pkg" / "deep" / "conftest.py").write_text("# de la base\n")
    return base


def test_restore_does_not_cross_ancestor_symlink_for_tests(tmp_path):
    base = _nested_base(tmp_path)
    outside = tmp_path / "outside"
    (outside / "tests").mkdir(parents=True)
    (outside / "tests" / "sentinel").write_text("intacto")
    pr = tmp_path / "pr"
    pr.mkdir()
    os.symlink(outside, pr / "pkg")
    sandbox.restore_eval_files(pr, base, "pkg/tests")
    assert (outside / "tests" / "sentinel").read_text() == "intacto"
    assert not (outside / "tests" / "test_original.py").exists()
    assert sorted(p.name for p in outside.rglob("*")) == ["sentinel", "tests"]
    assert not (pr / "pkg").is_symlink() and (pr / "pkg" / "tests" / "test_original.py").is_file()


def test_restore_does_not_cross_ancestor_symlink_for_hooks(tmp_path):
    base = _nested_base(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "conftest.py").write_text("ajeno")
    pr = tmp_path / "pr"
    (pr / "pkg" / "tests").mkdir(parents=True)
    os.symlink(outside, pr / "pkg" / "deep")
    sandbox.restore_eval_files(pr, base, "pkg/tests")
    assert (outside / "conftest.py").read_text() == "ajeno"
    assert (pr / "pkg" / "deep" / "conftest.py").read_text() == "# de la base\n"
    assert not (pr / "pkg" / "deep").is_symlink()


def test_eval_workdir_rejects_ancestor_symlink(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "tests").mkdir(parents=True)
    base = tmp_path / "base"
    base.mkdir()
    os.symlink(elsewhere, base / "pkg")
    with pytest.raises(sandbox.SandboxError):
        sandbox.prepare_eval_workdir(base, "pkg/tests", tmp_path / "eval")


SKIPPED_HOLDOUT = suite(ok=False, skipped=["test_reserved::test_b"])


@pytest.mark.parametrize("res,code,expect_holdout", [
    (result("failed", base=suite(), holdout=SKIPPED_HOLDOUT), 1, True),       # reservados con omitidos
    (result("ok", base=suite(), holdout=SKIPPED_HOLDOUT), 0, True),
    (result("failed", base=suite(), holdout=suite(ok=False)), 1, True),         # no aprobó y no dice por qué
    (result("ok", base=suite(), holdout=None), 0, True),                        # reservados obligatorios ausentes
    (result("failed", base=suite()), 1, False),                                 # estado failed sin detalle
    (result("ok", base=suite()), 1, False),                                     # código de salida no coincide
    (result("ok", base=suite()), None, False),
    (result("ok", base=suite(ok=False)), 0, False),
    (result("ok", base=suite(passed=0)), 0, False),
    (result("raro", base=suite()), 0, False),
    ({"status": "ok", "errors": [], "suites": {"base": suite()}}, 0, False),    # sin esquema
    ({"schema": 2, "status": "ok", "errors": [], "suites": {"base": suite()}}, 0, False),
    ({"schema": 1, "errors": [], "suites": {"base": suite()}}, 0, False),       # sin estado
    ({"schema": 1, "status": "ok", "errors": [], "suites": {"base": {"ok": True}}}, 0, False),
    ({"schema": 1, "status": "ok", "errors": [], "suites": {"base": suite(ok="true")}}, 0, False),
    ({"schema": 1, "status": "ok", "errors": [], "suites": []}, 0, False),
    (result("ok", base=suite(), attacks={"confirmed": []}), 0, False),
    (None, 0, False),
    ("ok", 0, False),
])
def test_evaluator_rejection_is_never_lost(res, code, expect_holdout):
    ev = pipeline.evidence_from(res, [], code, expect_holdout)
    assert ev.incomplete and not decide(ev).approved


def test_evidence_ok_with_required_holdout():
    ev = pipeline.evidence_from(result("ok", base=suite(), holdout=suite()), [], 0, True)
    assert not ev.incomplete and decide(ev).approved


def test_comment_or_docstring_mentioning_pytest_is_not_flagged(repo):
    sha = branch(repo, "pr", {"app/main.py": '"""Se prueba con pytest."""\n# Ejecutar con pytest antes del merge.\ndef f():\n    """Ver pytest."""\n    return 2\n'})
    assert scan(repo, sha) == []


@pytest.mark.parametrize("code", [
    "import pytest\n",
    "from _pytest import reports\n",
    "import sys\nm = sys.modules.get('pytest')\n",
    "import importlib\nm = importlib.import_module('_pytest.reports')\n",
    "def pytest_runtest_makereport(item, call):\n    pass\n",
    "import os\nx = os.environ.get('PYTEST_CURRENT_TEST')\n",
    "import pluggy\n",
])
def test_executable_pytest_reference_blocks(repo, code):
    flags = scan(repo, branch(repo, "pr", {"app/extra.py": code}))
    assert flags and all(f.severity == "block" and f.line for f in flags)


def test_unparseable_file_falls_back_to_line_scan(repo):
    flags = scan(repo, branch(repo, "pr", {"app/extra.py": "def (:\n# pytest\n"}))
    assert [f.line for f in flags] == [2]
