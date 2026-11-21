"""Plugin del evaluador: suites con TestClient en el mismo proceso, corridas por HTTP sin tocarlas."""

import http.server
import json
import threading

import pytest

from cachai.evaluator import run_suite


class _App(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"/health": {"ok": True}, "/items/1": {"id": 1, "name": "uno"}}.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body or {"detail": "no"}).encode())

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def base_url():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _App)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


IN_PROCESS = '''
from fastapi.testclient import TestClient
from miapp.main import app, helper

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"ok": True}


def test_item():
    r = client.get("/items/1")
    assert r.status_code == 200 and r.json()["name"] == "uno"


def test_missing_item():
    assert client.get("/items/9").status_code == 404


def test_uses_internals():
    assert helper(2) == 4
'''

OVERRIDES = '''
import pytest
from fastapi.testclient import TestClient
from miapp.main import app
from miapp.deps import get_db


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = lambda: None
    with TestClient(app) as c:
        yield c


def test_with_override(client):
    assert client.get("/health").status_code == 200
'''

MODULE_LEVEL = '''
from miapp.db import engine

engine.connect()


def test_never_runs():
    pass
'''


def test_in_process_suite_runs_over_http_and_lists_what_cannot(tmp_path, base_url):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text(IN_PROCESS)
    (tmp_path / "tests" / "test_overrides.py").write_text(OVERRIDES)
    (tmp_path / "tests" / "test_module.py").write_text(MODULE_LEVEL)
    s = run_suite(base_url, tmp_path, "tests", 60)
    assert s["passed"] == 3, s["log_tail"]
    assert not s["failed"] and not s["skipped"], s
    outside = {x["nodeid"].rsplit("::", 1)[-1] for x in s["not_external"]}
    assert {"test_uses_internals", "test_with_override"} <= outside
    assert all("no está en el evaluador" in x["reason"] for x in s["not_external"])
    assert s["ok"]


def test_a_real_failure_is_still_a_failure(tmp_path, base_url):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text(
        "from fastapi.testclient import TestClient\nfrom miapp.main import app\n\n"
        "def test_wrong():\n    assert TestClient(app).get('/items/1').json()['name'] == 'dos'\n")
    s = run_suite(base_url, tmp_path, "tests", 60)
    assert s["failed"] and not s["ok"] and not s["not_external"]
