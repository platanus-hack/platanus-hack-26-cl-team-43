"""Corre los tests de `action/publish.js` (el paso que comenta en el PR) con `node --test`, si hay node."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="requiere node")
def test_publish_js():
    p = subprocess.run(["node", "--test", str(ROOT / "action" / "publish.test.js")], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
