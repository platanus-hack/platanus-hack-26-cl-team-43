"""`cachai check` local: sin material reservado corre en modo visible y lo avisa (#12)."""

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from cachai import cli, pipeline
from cachai.case import load_case

FACTURAS = Path(__file__).resolve().parent.parent / "cases" / "facturas"


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_check(repo, base, pr, mode, holdout_tests=None, budget=120.0, plan=None, allow_missing_hidden=False):
        seen.append({"mode": mode, "holdout": holdout_tests, "plan": plan})
        confirmed = [plan.split.visible[0], plan.split.reserved[0]] if plan else []
        ev = {"attacks": {"confirmed": confirmed}} if plan else None
        return pipeline.CheckResult(mode, "b" * 40, "p" * 40, not confirmed, ["motivo"] if confirmed else [], evaluator=ev)

    for k in cli.CI_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(pipeline, "check", fake_check)
    monkeypatch.setattr(cli, "local_base_url", lambda repo, base: "http://localhost:8000")
    return seen


def run(capsys, *argv):
    code = cli.main(["check", "--pr", "rama", "--color", "never", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_without_reserved_material_runs_visible_and_says_so(calls, capsys):
    code, out, err = run(capsys)
    assert calls[-1]["mode"] == "visible"
    assert err.count(cli.VISIBLE_WARNING) == 2  # antes de correr y al final, donde se lee
    assert "\033[" not in err  # stderr no es una terminal: sin colores
    assert code == 0 and "APROBADO" in out


def test_in_ci_the_mode_must_be_explicit(calls, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    code, _, err = run(capsys)
    assert code == 2 and "--mode" in err and calls == []
    run(capsys, "--mode", "full")
    assert calls[-1]["mode"] == "full"


def test_explicit_full_without_material_has_no_warning(calls, capsys):
    _, _, err = run(capsys, "--mode", "full")
    assert calls[-1]["mode"] == "full" and cli.VISIBLE_WARNING not in err


def test_with_reserved_tests_defaults_to_full_without_warning(calls, capsys, tmp_path):
    _, _, err = run(capsys, "--holdout-tests", str(tmp_path))
    assert calls[-1]["mode"] == "full" and calls[-1]["holdout"] == tmp_path
    assert cli.VISIBLE_WARNING not in err


def test_explicit_mode_wins(calls, capsys):
    run(capsys, "--mode", "ci")
    assert calls[-1]["mode"] == "ci"


def test_json_output_stays_parseable(calls, capsys):
    _, out, err = run(capsys, "--json")
    assert json.loads(out)["mode"] == "visible"
    assert cli.VISIBLE_WARNING in err


def test_reproduce_only_visible_attacks(calls, capsys):
    code, out, _ = run(capsys, "--attacks", str(FACTURAS / "attacks.json"))
    plan = calls[-1]["plan"]
    by_id = {a.id: a for a in plan.attacks}
    visible, reserved = by_id[plan.split.visible[0]], by_id[plan.split.reserved[0]]
    assert code == 1
    assert "Para reproducir" in out
    lines = [line.strip() for line in out.splitlines() if line.strip().startswith("curl ")]
    assert len(lines) == 1
    assert shlex.quote(visible.request["path"]) in lines[0] and "${BASE_URL:-http://localhost:8000}" in lines[0]
    assert reserved.request["path"] not in out


def _words(line: str, base_url: str = "http://app") -> list[str]:
    """Lo que recibiría curl, según el shell."""
    out = subprocess.run(["sh", "-c", 'printf "%s\\n" ' + line[len("curl "):]], capture_output=True, text=True,
                         env={"BASE_URL": base_url})
    return out.stdout.split("\n")[:-1]


def test_reproduce_command_is_valid_shell(calls, capsys):
    _, out, _ = run(capsys, "--attacks", str(FACTURAS / "attacks.json"))
    plan = calls[-1]["plan"]
    visible = next(a for a in plan.attacks if a.id == plan.split.visible[0])
    line = next(line.strip() for line in out.splitlines() if line.strip().startswith("curl "))
    words = _words(line)
    assert words[-1] == "http://app" + visible.request["path"]
    for k, v in visible.request["headers"].items():
        assert f"{k}: {v}" in words


def test_curl_keeps_awkward_requests_intact():
    req = {"method": "POST", "path": "/a/../b/%2F[x]?q=1&r='2'",
           "headers": {"Authorization": "Bearer it's", "content-type": "application/json"},
           "json": {"nota": "dice \"hola\" & 'chao'"}}
    words = _words(cli.curl_command(req, "http://localhost:8000"))
    assert words[-1] == "http://app" + req["path"]
    assert {"--path-as-is", "--globoff", "-L"} <= set(words)
    assert "Authorization: Bearer it's" in words
    assert sum(w.lower().startswith("content-type") for w in words) == 1  # no se duplica
    assert json.loads(words[words.index("--data") + 1]) == req["json"]
    assert _words(cli.curl_command(req, "http://localhost:8000"), "")[-1] == "http://localhost:8000" + req["path"]
    assert "-I" in _words(cli.curl_command({"method": "HEAD", "path": "/x"}, "http://h"))


def test_local_split_matches_the_bench(calls, capsys):
    """Con los valores por defecto de --shown y --seed, el check local ve los mismos
    ataques visibles que el modo visible del bench: el resultado coincide por construcción."""
    run(capsys, "--attacks", str(FACTURAS / "attacks.json"))
    assert calls[-1]["plan"].split == load_case(FACTURAS).split()
