"""Familia `cmdi`: un comando inyectado deja un archivo testigo con nombre aleatorio (#28)."""

import os
import subprocess
import time
import uuid

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI, Query  # noqa: E402

from cachai import redteam  # noqa: E402
from tests.test_authn import Served  # noqa: E402

INV = redteam.Inventory.from_dict({
    "users": {"ana": {"Authorization": "Bearer tok-ana"}, "beto": {"Authorization": "Bearer tok-beto"}},
    "records": [{"id": "1", "owner": "ana", "markers": ["CNR-ANA-1"]}],
})
ROUTES = [("/ping", [], ["host"]), ("/notes/{note_id}", ["note_id"], [])]


def ping_app(shell: bool) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/ping")
    def ping(host: str = Query(...)):
        if shell:
            r = subprocess.run(f"echo ping {host}", shell=True, capture_output=True, text=True)
        else:
            r = subprocess.run(["echo", "ping", host], capture_output=True, text=True)
        return {"out": r.stdout}

    return app


def _found(app) -> list:
    nonce = uuid.uuid4().hex[:12]
    probes = redteam.cmdi_probes(INV, ROUTES, nonce)
    with Served(app) as srv:
        redteam.send_probes(srv.url, probes, time.monotonic() + 30)
    present = {f for f in os.listdir(redteam.CMDI_DIR) if f.endswith(nonce)}
    try:
        return redteam.cmdi_attacks(probes, present)
    finally:
        for f in present:
            os.remove(os.path.join(redteam.CMDI_DIR, f))


def test_shell_true_is_detected():
    found = _found(ping_app(shell=True))
    assert found and {a.family for a in found} == {"cmdi"}
    assert {a.group for a in found} >= {"semicolon", "and", "subshell", "backtick"}
    assert all(a.request["path"].startswith("/ping?host=") for a in found)


def test_argument_list_gives_no_false_alarm():
    assert _found(ping_app(shell=False)) == []


def test_probe_ids_are_stable_and_files_change_every_run():
    a, b = redteam.cmdi_probes(INV, ROUTES, "n1"), redteam.cmdi_probes(INV, ROUTES, "n2")
    assert [p["id"] for p in a] == [p["id"] for p in b]
    assert all(p["file"] != q["file"] for p, q in zip(a, b))
    assert all("/notes/" not in p["request"]["path"] for p in a)  # sin tramos de ruta
