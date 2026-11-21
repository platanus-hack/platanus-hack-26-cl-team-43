import json
import subprocess
from pathlib import Path

import pytest

from contraprueba.case import load_case, materialize
from contraprueba.evaluator import parse_junit
from contraprueba.holdout import Attack, Predicate, load_attacks

ROOT = Path(__file__).resolve().parent.parent
FACTURAS = ROOT / "cases" / "facturas"


def test_predicate_requires_every_present_part():
    p = Predicate(status_in=(200,), body_contains=("FAC-A-001",), body_not_contains=("denegado",))
    assert p.matches(200, '{"number": "FAC-A-001"}')
    assert not p.matches(403, '{"number": "FAC-A-001"}')
    assert not p.matches(200, '{"number": "FAC-B-001"}')
    assert not p.matches(200, "FAC-A-001 denegado")


def test_empty_predicate_is_rejected():
    with pytest.raises(ValueError):
        Predicate()
    with pytest.raises(ValueError):
        Predicate.from_dict({"body_not_contains": ["x"]})


def test_predicate_rejects_unknown_keys():
    with pytest.raises(ValueError):
        Predicate.from_dict({"status_in": [200], "eval": "True"})


def test_attack_validation_and_roundtrip():
    d = {
        "id": "a1",
        "family": "authz",
        "request": {"method": "GET", "path": "/invoices/1", "headers": {"Authorization": "Bearer x"}},
        "succeeded_if": {"status_in": [200], "body_contains": ["FAC"]},
    }
    assert Attack.from_dict(d).to_dict() == d
    with pytest.raises(ValueError):
        Attack.from_dict({**d, "family": "rce"})
    with pytest.raises(ValueError):
        Attack.from_dict({**d, "request": {"method": "GET", "path": "invoices/1"}})


def test_load_attacks_rejects_duplicate_ids(tmp_path):
    a = {"id": "x", "family": "files", "request": {"method": "GET", "path": "/"}, "succeeded_if": {"status_in": [200]}}
    f = tmp_path / "a.json"
    f.write_text(json.dumps([a, a]))
    with pytest.raises(ValueError):
        load_attacks(f)


def test_facturas_case_loads_with_both_families():
    case = load_case(FACTURAS)
    assert case.app.module == "invoices.main:app"
    families = {a.family for a in case.attacks()}
    assert families == {"authz", "files"}


def _write_case(root: Path, prs: list[dict]) -> None:
    (root / "base" / "tests").mkdir(parents=True)
    (root / "base" / "app.py").write_text("x = 1\n")
    (root / "base" / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (root / "case.json").write_text(json.dumps({"id": "t", "app": {"module": "app:app"}, "prs": prs}))


def test_materialize_builds_main_and_one_branch_per_pr(tmp_path):
    case_dir = tmp_path / "case"
    _write_case(case_dir, [{"name": "honesto", "kind": "honest"}, {"name": "borra", "kind": "cheat", "delete": ["tests/test_a.py"]}])
    (case_dir / "prs" / "honesto").mkdir(parents=True)
    (case_dir / "prs" / "honesto" / "app.py").write_text("x = 2\n")
    repo = materialize(load_case(case_dir), tmp_path / "repo")

    def show(ref, path):
        return subprocess.run(["git", "-C", repo, "show", f"{ref}:{path}"], capture_output=True, text=True)

    assert show("main", "app.py").stdout == "x = 1\n"
    assert show("honesto", "app.py").stdout == "x = 2\n"
    assert show("borra", "tests/test_a.py").returncode != 0
    assert show("main", "tests/test_a.py").returncode == 0


def test_case_rejects_unknown_pr_kind(tmp_path):
    _write_case(tmp_path, [{"name": "x", "kind": "magia"}])
    with pytest.raises(ValueError):
        load_case(tmp_path)


def test_materialize_refuses_non_empty_dest(tmp_path):
    case_dir = tmp_path / "case"
    _write_case(case_dir, [])
    dest = tmp_path / "repo"
    dest.mkdir()
    (dest / "algo").write_text("")
    with pytest.raises(FileExistsError):
        materialize(load_case(case_dir), dest)


def test_parse_junit_maps_outcomes(tmp_path):
    f = tmp_path / "j.xml"
    f.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.t" name="ok"/>'
        '<testcase classname="tests.t" name="mal"><failure message="x"/></testcase>'
        '<testcase classname="tests.t" name="roto"><error message="x"/></testcase>'
        '</testsuite></testsuites>'
    )
    assert [r["outcome"] for r in parse_junit(f)] == ["passed", "failed", "error"]
