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
