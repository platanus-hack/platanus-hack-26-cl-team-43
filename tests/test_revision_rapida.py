"""Revisión rápida: atajo de documentación, caché del equipo rojo contra la base y tiempos.

Lo que no necesita Docker se prueba acá; los contenedores se miden a mano con `scripts/demo.sh`.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from cachai import gitio, pipeline, sandbox
from cachai.case import CONFIG_FILE
from cachai.holdout import Attack, Predicate

ROOT = Path(__file__).resolve().parent.parent
CONFIG = {"app": {"module": "app.main:app"}}


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "app").mkdir(parents=True)
    (r / "app" / "__init__.py").write_text("")
    (r / "app" / "main.py").write_text("def f():\n    return 1\n")
    (r / "tests").mkdir()
    (r / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    (r / "pyproject.toml").write_text('[project]\nname = "x"\n')
    (r / "README.md").write_text("# app\n")
    (r / CONFIG_FILE).write_text(json.dumps(CONFIG))
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    return r


def branch(repo, files=None, delete=(), symlinks=None):
    git(repo, "checkout", "-q", "-b", "pr", "main")
    for path, content in (files or {}).items():
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    for path in delete:
        (repo / path).unlink()
    for path, target in (symlinks or {}).items():
        os.symlink(target, repo / path)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "pr")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return sha


def docs(repo, pr_sha):
    base_sha = gitio.resolve(repo, "main")
    target = pipeline.target_from_base(repo, base_sha)
    return pipeline.docs_only_files(repo, base_sha, pr_sha, target, [])


# --- atajo de documentación -------------------------------------------------

def test_only_md_changes_are_a_docs_pr(repo):
    sha = branch(repo, {"docs/guia.md": "hola\n", "README.md": "# app\n\nmás\n"})
    assert docs(repo, sha) == ["README.md", "docs/guia.md"]


def test_code_change_is_not_a_docs_pr(repo):
    sha = branch(repo, {"README.md": "x\n", "app/main.py": "def f():\n    return 2\n"})
    assert docs(repo, sha) is None


def test_md_inside_the_tests_dir_is_not_a_docs_pr(repo):
    sha = branch(repo, {"tests/notas.md": "x\n"})
    assert docs(repo, sha) is None


def test_base_that_reads_md_blocks_the_shortcut(repo):
    (repo / "app" / "main.py").write_text("TEMPLATE = 'README.md'\n")
    git(repo, "commit", "-q", "-am", "la app lee md")
    sha = branch(repo, {"README.md": "# otro\n"})
    assert docs(repo, sha) is None


def test_docs_symlink_is_not_a_docs_pr(repo):
    sha = branch(repo, {"README.md": "x\n"}, symlinks={"NOTAS.md": "README.md"})
    assert docs(repo, sha) is None


def test_docs_pr_checks_without_starting_a_container(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("el atajo de documentación no debe levantar contenedores")

    monkeypatch.setattr(sandbox, "start_app", boom)
    monkeypatch.setattr(sandbox, "run_pytest_inside", boom)
    sha = branch(repo, {"docs/guia.md": "hola\n"})
    r = pipeline.check(repo, "main", sha, mode="full")
    assert r.approved and r.outcome == "approved"
    assert r.docs_only == ["docs/guia.md"]
    assert any("solo cambia documentación" in n for n in r.notes)
    assert r.timings["total"] >= 0 and "preparar" in r.timings
    report = r.report()
    assert report["checked"] and "solo cambia documentación" in report["checked"][0]
    assert "Tiempo:" in report["public"]["markdown"]


def test_checked_lines_refuses_a_contradictory_docs_report():
    with pytest.raises(ValueError):
        pipeline.checked_lines({"approved": True, "outcome": "approved", "docs_only": ["app/main.py"]})
    with pytest.raises(ValueError):
        pipeline.checked_lines({"approved": True, "outcome": "approved", "docs_only": ["README.md"], "inside": {}})


# --- caché del equipo rojo contra la base -----------------------------------

def test_base_hunt_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHAI_CACHE_DIR", str(tmp_path / "cache"))
    target = pipeline.target_from_base(*_tiny_repo(tmp_path))
    key = pipeline.base_hunt_key("a" * 40, target, None)
    assert pipeline.load_base_hunt(key, "a" * 40) is None
    pipeline.save_base_hunt(key, "a" * 40, {"authz-1", "files-2"})
    assert pipeline.load_base_hunt(key, "a" * 40) == {"authz-1", "files-2"}
    assert pipeline.load_base_hunt(key, "b" * 40) is None  # otra base: no se reusa
    mode = oct((tmp_path / "cache" / "rojo-base" / f"{key}.json").stat().st_mode)
    assert mode.endswith("600")


@pytest.mark.parametrize("content", ["[1]", '"x"', "null", '{"base": 1}', "{no es json"])
def test_corrupt_base_hunt_cache_is_ignored(tmp_path, monkeypatch, content):
    monkeypatch.setenv("CACHAI_CACHE_DIR", str(tmp_path / "cache"))
    f = tmp_path / "cache" / "rojo-base" / "k.json"
    f.parent.mkdir(parents=True)
    f.write_text(content)
    assert pipeline.load_base_hunt("k", "a" * 40) is None


@pytest.mark.parametrize("path", [
    "AGENTS.md", "CLAUDE.md", "docs/agents.md", "GEMINI.md", ".github/copilot-instructions.md",
    ".github/prompts/x.prompt.md", ".claude/commands/deploy.md", ".cursor/rules/a.md",
])
def test_agent_instruction_files_are_not_docs(path):
    assert not pipeline.is_doc(path)


def test_plain_markdown_is_doc():
    assert pipeline.is_doc("README.md") and pipeline.is_doc("docs/guia.md")


def test_base_hunt_key_depends_on_config_and_compose(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHAI_CACHE_DIR", str(tmp_path / "cache"))
    repo = _tiny_repo(tmp_path)
    target = pipeline.target_from_base(*repo)
    assert pipeline.base_hunt_key("a" * 40, target, b"services: {}") == pipeline.base_hunt_key("a" * 40, target, "services: {}")
    assert pipeline.base_hunt_key("a" * 40, target, b"x") != pipeline.base_hunt_key("a" * 40, target, b"y")
    assert pipeline.base_hunt_key("a" * 40, target, None) != pipeline.base_hunt_key("b" * 40, target, None)


def test_compare_red_team_accepts_cached_ids():
    ok = Predicate(status_in=(200,))
    attack = Attack("authz-1", "authz", {"method": "GET", "path": "/x/1"}, ok)
    other = Attack("authz-2", "authz", {"method": "GET", "path": "/x/2"}, ok)
    intro = pipeline.compare_red_team([attack, other], {"authz-1"})
    assert [a.id for a in intro.introduced] == ["authz-2"]
    assert [a.id for a in intro.existing] == ["authz-1"]


# --- imagen de dependencias -------------------------------------------------

def test_deps_key_follows_requirements_and_dockerfile():
    assert sandbox.deps_key(b"fastapi\n") == sandbox.deps_key(b"fastapi\n")
    assert sandbox.deps_key(b"fastapi\n") != sandbox.deps_key(b"fastapi>=0.1\n")
    assert sandbox.deps_key(b"x\n", "sha256:img") != sandbox.deps_key(b"x\n", "sha256:otra")


def test_local_requirements_are_detected():
    assert sandbox._local_requirement("-e .")
    assert sandbox._local_requirement("file:./paquete")
    assert not sandbox._local_requirement("fastapi==0.115.0")


def _tiny_repo(tmp_path):
    r = tmp_path / "tiny"
    (r / "app").mkdir(parents=True)
    (r / "app" / "__init__.py").write_text("")
    (r / "app" / "main.py").write_text("app = None\n")
    (r / CONFIG_FILE).write_text(json.dumps(CONFIG))
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    return r, gitio.resolve(r, "main")
