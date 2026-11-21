"""Paquete reservado cifrado: sellar, abrir, rechazar claves y paquetes alterados, y leerlo de la base."""

import json
import subprocess
from pathlib import Path

import pytest

from cachai import cli, hidden, pipeline
from cachai.case import CONFIG_FILE, load_case

ROOT = Path(__file__).resolve().parent.parent
FACTURAS = ROOT / "cases" / "facturas"
KEY = "k" * 64


@pytest.fixture(scope="module")
def sealed():
    case = load_case(FACTURAS)
    return hidden.seal(KEY, FACTURAS / case.attacks_file, case.holdout_tests_path, case.split_shown, case.split_seed)


def test_roundtrip_keeps_attacks_split_and_tests(sealed, tmp_path):
    case = load_case(FACTURAS)
    m = hidden.open_package(sealed, KEY, tmp_path / "r")
    assert [a.id for a in m.attacks] == [a.id for a in case.attacks()]
    assert m.split == case.split()
    assert (m.holdout_tests / "test_funcional.py").read_bytes() == (case.holdout_tests_path / "test_funcional.py").read_bytes()
    assert oct((tmp_path / "r").stat().st_mode & 0o777) == "0o700"
    # Adentro, legible por otro usuario: el evaluador corre con un uid distinto al del runner.
    assert (m.holdout_tests / "test_funcional.py").stat().st_mode & 0o004
    assert m.holdout_tests.stat().st_mode & 0o005 == 0o005


def test_package_is_not_readable_without_the_key(sealed):
    for secret in (b"FAC-A-001", b"test_funcional", b"tok-alice", b"attachments"):
        assert secret not in sealed


@pytest.mark.parametrize("key", [None, "", "corta", "x" * 64])
def test_wrong_or_missing_key_does_not_open(sealed, tmp_path, key):
    with pytest.raises(hidden.HiddenError):
        hidden.open_to_dir(sealed, key, tmp_path / "r")
    assert not (tmp_path / "r").exists()


def test_tampered_package_does_not_open(sealed, tmp_path):
    bad = bytearray(sealed)
    bad[-20] ^= 1
    with pytest.raises(hidden.HiddenError, match="alterado"):
        hidden.open_package(bytes(bad), KEY, tmp_path / "r")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, sealed):
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    (r / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    (r / ".cachai").mkdir()
    (r / ".cachai" / "reservado.cachai").write_bytes(sealed)
    (r / CONFIG_FILE).write_text(json.dumps({"version": 2, "app": {"module": "app:app"},
                                             "hidden": {"package": ".cachai/reservado.cachai", "key_env": "CACHAI_TEST_KEY"}}))
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def test_package_is_read_from_the_base_commit_not_the_pr(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CACHAI_TEST_KEY", KEY)
    _git(repo, "checkout", "-q", "-b", "pr")
    (repo / ".cachai" / "reservado.cachai").write_bytes(b"basura del PR")
    _git(repo, "commit", "-q", "-am", "pr")
    base = _git(repo, "rev-parse", "main")
    target = pipeline.target_from_base(repo, base)
    plan, tests = pipeline.load_hidden(repo, base, target, tmp_path / "r")
    assert plan.split.reserved and (tests / "test_funcional.py").is_file()


def test_missing_key_makes_the_check_incomplete_not_approved(repo, monkeypatch):
    monkeypatch.delenv("CACHAI_TEST_KEY", raising=False)
    r = pipeline.check(repo, "main", "main", "full")
    assert not r.approved and r.outcome == "incomplete" and "material reservado" in r.reasons[0]


def test_cli_seal_and_open(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CACHAI_HIDDEN_KEY", KEY)
    out = tmp_path / "p.cachai"
    assert cli.main(["hidden", "seal", "--attacks", str(FACTURAS / "attacks.json"), "--holdout-tests", str(FACTURAS / "reservados"), "--out", str(out)]) == 0
    assert cli.main(["hidden", "open", str(out), "--out", str(tmp_path / "abierto")]) == 0
    assert "ataques" in capsys.readouterr().out
    monkeypatch.setenv("CACHAI_HIDDEN_KEY", "y" * 64)
    assert cli.main(["hidden", "open", str(out), "--out", str(tmp_path / "otra")]) == 2


def test_pr_that_edits_the_ci_workflow_is_blocked():
    from cachai.integrity import classify
    (f,) = classify([("M", ".github/workflows/cachai.yml")])
    assert f.severity == "block" and "workflow" in f.reason


# --- cachai check local (#12) con paquete reservado --------------------------

def _args(repo, **kw):
    import argparse
    return argparse.Namespace(mode=None, holdout_tests=None, repo=str(repo), base="main", **kw)


def test_local_default_is_full_only_with_the_key(repo, monkeypatch):
    for k in cli.CI_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("CACHAI_TEST_KEY", raising=False)
    assert cli.check_mode(_args(repo)) == "visible"
    monkeypatch.setenv("CACHAI_TEST_KEY", KEY)
    assert cli.check_mode(_args(repo)) == "full"


@pytest.mark.skipif(__import__("os").environ.get("CP_DOCKER") != "1", reason="requiere CP_DOCKER=1")
def test_local_visible_without_the_key_is_not_incomplete(tmp_path, monkeypatch):
    """En la máquina del dev no está la clave: el modo visible sigue sin el paquete y lo anota."""
    from cachai.case import materialize
    monkeypatch.delenv("CACHAI_TEST_KEY", raising=False)
    case = load_case(Path(__file__).resolve().parent.parent / "cases" / "facturas")
    repo = materialize(case, tmp_path / "facturas")
    r = pipeline.check(repo, "main", "honesto", "visible", target=_with_hidden(case.target()), allow_missing_hidden=True)
    assert r.outcome == "approved", r.reasons
    full = pipeline.check(repo, "main", "honesto", "full", target=_with_hidden(case.target()))
    assert full.outcome == "incomplete"  # en full, sin clave, no hay veredicto
    assert any("CACHAI_TEST_KEY" in n for n in r.notes)


def _with_hidden(target):
    from dataclasses import replace

    from cachai.case import Hidden
    return replace(target, hidden=Hidden(".cachai/reservado.cachai", "CACHAI_TEST_KEY"))


def test_visible_without_the_key_is_incomplete_unless_it_is_the_local_review(repo, monkeypatch):
    """En CI (o llamando al pipeline sin pedirlo) el modo visible sin clave no se salta el material reservado."""
    monkeypatch.delenv("CACHAI_TEST_KEY", raising=False)
    r = pipeline.check(repo, "main", "main", "visible")
    assert r.outcome == "incomplete" and "material reservado" in r.reasons[0]


@pytest.mark.parametrize("ci, allowed", [({}, True), ({"CI": "true"}, False), ({"GITHUB_ACTIONS": "true"}, False)])
def test_cli_allows_missing_key_only_outside_ci(repo, monkeypatch, ci, allowed):
    seen = {}

    def fake_check(*a, **k):
        seen.update(k)
        return pipeline.CheckResult("visible", "a" * 40, "b" * 40, True)

    for k in cli.CI_ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in ci.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(pipeline, "check", fake_check)
    cli.main(["check", "--repo", str(repo), "--pr", "main", "--mode", "visible", "--color", "never"])
    assert seen["allow_missing_hidden"] is allowed
