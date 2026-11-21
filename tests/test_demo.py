"""El guion de la demo comprueba cada paso: no anuncia éxito si algo falló."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "scripts" / "demo.sh"

FAKE = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
pr, mode = args[args.index("--pr") + 1], args[args.index("--mode") + 1]
behaviour = os.environ["FAKE_BEHAVIOUR"]
if behaviour == "crash":
    sys.exit(2)
expected = {("trampa-paquete", "full"): "rejected", ("parche-a-medias", "full"): "rejected"}.get((pr, mode), "approved")
outcome = expected
if behaviour == "flip" and (pr, mode) == ("parche-a-medias", "visible"):
    outcome = "rejected"
if behaviour == "incomplete" and (pr, mode) == ("trampa-paquete", "full"):
    outcome = "incomplete"
if "--report" in args:
    json.dump({"outcome": outcome}, open(args[args.index("--report") + 1], "w"))
print("salida de", pr, mode)
if behaviour == "lying-exit" and (pr, mode) == ("parche-correcto", "full"):
    sys.exit(1)  # el reporte dice approved, pero el comando terminó mal
if behaviour == "lying-zero" and (pr, mode) == ("trampa-paquete", "full"):
    sys.exit(0)  # el reporte dice rejected, pero el comando salió con 0
sys.exit(0 if outcome == "approved" else 1)
"""


@pytest.fixture
def fake(tmp_path):
    script = tmp_path / "cachai-falso"
    script.write_text(FAKE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def demo(fake, behaviour, mode):
    env = {**os.environ, "CACHAI": str(fake), "FAKE_BEHAVIOUR": behaviour, "TERM": "dumb"}
    return subprocess.run(["bash", str(DEMO), mode], capture_output=True, text=True, env=env, timeout=120)


def test_warm_succeeds_only_when_every_verdict_is_the_scripted_one(fake):
    ok = demo(fake, "ok", "--warm")
    assert ok.returncode == 0 and ok.stdout.count("listo:") == 6 and "son los del guion" in ok.stdout


@pytest.mark.parametrize("behaviour,where", [("crash", "trampa-paquete en modo ci"), ("flip", "parche-a-medias en modo visible"),
                                             ("incomplete", "trampa-paquete en modo full"),
                                             ("lying-exit", "parche-correcto en modo full"), ("lying-zero", "trampa-paquete en modo full")])
def test_warm_stops_on_errors_unexpected_or_incomplete_verdicts(fake, behaviour, where):
    r = demo(fake, behaviour, "--warm")
    assert r.returncode != 0 and "son los del guion" not in r.stdout
    assert "ERROR" in r.stderr and where in r.stderr


def test_live_demo_stops_instead_of_showing_a_wrong_verdict(fake):
    assert demo(fake, "ok", "--auto").returncode == 0
    r = demo(fake, "flip", "--auto")
    assert r.returncode != 0 and "Segundo parche" not in r.stdout and "ERROR" in r.stderr


@pytest.mark.parametrize("behaviour", ["lying-exit", "lying-zero"])
def test_live_demo_requires_exit_code_and_report_to_agree(fake, behaviour):
    r = demo(fake, behaviour, "--auto")
    assert r.returncode != 0 and "ERROR" in r.stderr
