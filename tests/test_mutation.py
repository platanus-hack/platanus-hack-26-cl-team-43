"""Aviso de tests débiles con mutantes de seguridad (#19): se informa, nunca se rechaza."""

import ast
import json
import subprocess
from pathlib import Path

import pytest

from cachai import mutation, pipeline
from cachai.case import CONFIG_FILE, load_target

APP = '''
from fastapi import FastAPI, Header, HTTPException

app = FastAPI()
USERS = {"tok-a": "alice", "tok-b": "bob"}
ITEMS = {1: {"id": 1, "owner": "alice"}, 2: {"id": 2, "owner": "bob"}}


def current_user(authorization: str | None) -> str:
    user = USERS.get((authorization or "").removeprefix("Bearer "))
    if user is None:
        raise HTTPException(status_code=401)
    return user


@app.get("/items")
def list_items(authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    return [i for i in ITEMS.values() if i["owner"] == user]


@app.get("/items/{item_id}")
def read_item(item_id: int, authorization: str | None = Header(default=None)):
    current_user(authorization)
    item = ITEMS[item_id]
    if not item["owner"]:
        raise HTTPException(status_code=404)
    return item


@app.delete("/items/{item_id}")
def delete_item(item_id: int, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    if ITEMS[item_id]["owner"] != user:
        raise HTTPException(status_code=403, detail="no")
    del ITEMS[item_id]
    return {"ok": True}
'''


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "app"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "main.py").write_text(APP)
    return root


def test_finds_auth_calls_and_owner_guards(tree):
    found = mutation.mutants(tree)
    kinds = {m.kind for m in found}
    assert "auth" in kinds and "owner" in kinds
    auth = [m for m in found if m.kind == "auth"]
    assert {m.function for m in auth} == {"read_item"}  # solo la llamada suelta, no la asignación
    owner = next(m for m in found if m.kind == "owner" and m.function == "delete_item")
    assert "owner" in owner.what


def test_apply_removes_only_the_chosen_auth_call(tree, tmp_path):
    mutant = next(m for m in mutation.mutants(tree) if m.kind == "auth")
    out = mutation.apply(tree, mutant, tmp_path / "mutado")
    src = (out / mutant.file).read_text()
    node = ast.parse(src)
    bare = [n for n in ast.walk(node) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "id", "") == "current_user"]
    assert bare == []  # se quitó la llamada suelta de read_item
    assert "user = current_user(authorization)" in src  # la asignación de list_items no se toca
    names = {n.name for n in ast.walk(node) if isinstance(n, ast.FunctionDef)}
    assert names >= {"list_items", "read_item", "delete_item"}


def test_apply_disables_only_the_chosen_guard(tree, tmp_path):
    mutant = next(m for m in mutation.mutants(tree) if m.kind == "owner")
    out = mutation.apply(tree, mutant, tmp_path / "mutado")
    src = (out / mutant.file).read_text()
    assert "if False:" in src
    assert 'ITEMS[item_id]["owner"] != user' not in src


def test_counts_mutants_once_per_file_and_line(tree):
    found = mutation.mutants(tree)
    assert len({(m.file, m.line) for m in found}) == len(found)


def _target(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / CONFIG_FILE).write_text(json.dumps({"app": {"module": "pkg.main:app"}}))
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], check=True)
    from cachai import gitio
    return pipeline.target_from_base(repo, gitio.resolve(repo, "main"))


OK = {"status": "ok", "suites": {"base": {"ok": True}, "holdout": {"ok": True}}}
FAILED = {"status": "failed", "suites": {"base": {"ok": False}, "holdout": {"ok": True}}}


@pytest.fixture
def fake_sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("CACHAI_CACHE_DIR", str(tmp_path / "cache"))
    calls = {"start": 0, "eval": 0}
    result = {"value": OK}

    def start(*a, **k):
        calls["start"] += 1
        return object()

    def evaluate(*a, **k):
        calls["eval"] += 1
        return 0, result["value"]

    monkeypatch.setattr(mutation.sandbox, "start_app", start)
    monkeypatch.setattr(mutation.sandbox, "stop", lambda *a, **k: None)
    monkeypatch.setattr(mutation.sandbox, "run_evaluator", evaluate)
    return calls, result


def _notes(tree, target, tmp_path):
    return mutation.notes(tree, target, tmp_path / "eval", None, 30.0, tmp_path / "work",
                          {"trusted_deps": tree}, "a" * 40, None)


def test_survivors_are_listed_as_a_note(tree, tmp_path, fake_sandbox):
    calls, _ = fake_sandbox
    target = _target(tmp_path)
    notes = _notes(tree, target, tmp_path)
    assert any("sobreviven" in n for n in notes)
    assert any("quita el control de sesión" in n for n in notes)
    assert calls["eval"] == len(mutation.mutants(tree))


def test_killed_mutants_are_a_good_news_note(tree, tmp_path, fake_sandbox):
    calls, result = fake_sandbox
    result["value"] = FAILED
    target = _target(tmp_path)
    notes = _notes(tree, target, tmp_path)
    assert len(notes) == 1 and "matan los" in notes[0]
    assert calls["eval"] == len(mutation.mutants(tree))


def test_result_is_cached_per_base(tree, tmp_path, fake_sandbox):
    calls, _ = fake_sandbox
    target = _target(tmp_path)
    first = _notes(tree, target, tmp_path)
    second = _notes(tree, target, tmp_path)
    assert first == second and calls["eval"] == len(mutation.mutants(tree))
    files = list((tmp_path / "cache" / "mutantes").glob("*.json"))
    assert files and oct(files[0].stat().st_mode & 0o777) == "0o600"


def test_cache_key_changes_with_the_base(tree, tmp_path, fake_sandbox):
    target = _target(tmp_path)
    assert mutation.cache_key("a" * 40, target, None, False) != mutation.cache_key("b" * 40, target, None, False)
    assert mutation.cache_key("a" * 40, target, b"x", False) != mutation.cache_key("a" * 40, target, b"y", False)
    assert mutation.cache_key("a" * 40, target, None, False) != mutation.cache_key("a" * 40, target, None, True)


def test_a_broken_mutant_does_not_count_as_a_survivor(tree, tmp_path, fake_sandbox):
    calls, result = fake_sandbox
    result["value"] = {"status": "error", "suites": {"base": {"ok": True, "complete": False}}}
    target = _target(tmp_path)
    notes = _notes(tree, target, tmp_path)
    assert "matan los" in notes[0]
