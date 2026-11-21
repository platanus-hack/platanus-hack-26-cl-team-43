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


def test_override_then_fails(client):
    assert client.get("/items/2").status_code == 200  # en el proceso, la base falsa lo tenía
'''

MODULE_LEVEL = '''
from miapp.db import engine

if engine.connect():
    pass


def test_never_runs():
    pass
'''


def test_in_process_suite_runs_over_http_and_lists_what_cannot(tmp_path, base_url):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text(IN_PROCESS)
    (tmp_path / "tests" / "test_overrides.py").write_text(OVERRIDES)
    (tmp_path / "tests" / "test_module.py").write_text(MODULE_LEVEL)
    s = run_suite(base_url, tmp_path, "tests", 60)
    assert s["passed"] == 4, s["log_tail"]
    assert not s["failed"] and not s["skipped"], s
    outside = {x["nodeid"].rsplit("::", 1)[-1] for x in s["not_external"]}
    assert {"test_uses_internals", "test_override_then_fails"} <= outside
    assert "test_with_override" not in outside
    assert all("no está en el evaluador" in x["reason"] for x in s["not_external"])
    assert s["ok"]


def test_a_real_failure_is_still_a_failure(tmp_path, base_url):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text(
        "from fastapi.testclient import TestClient\nfrom miapp.main import app\n\n"
        "def test_wrong():\n    assert TestClient(app).get('/items/1').json()['name'] == 'dos'\n")
    s = run_suite(base_url, tmp_path, "tests", 60)
    assert s["failed"] and not s["ok"] and not s["not_external"]


CONFTEST = '''
import pytest
from fastapi.testclient import TestClient
from sqlalchemy_no_instalado import create_engine
from miapp.main import create_app
from miapp.config import get_settings

engine = create_engine("sqlite://")


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def db():
    engine.metadata.create_all()
    yield engine
'''

FACTORY = '''
from miapp.main import helper
from miapp.config import get_settings


def test_ok(client):
    assert client.get("/health").json() == {"ok": True}


def test_real_failure(client):
    assert client.get("/items/1").json()["name"] == "dos"


def test_fails_after_seeding(client, db):
    db.insert({"id": 2})
    assert client.get("/items/2").status_code == 200


def test_reads_settings(client):
    assert client.get("/items/" + get_settings().item_id).status_code == 200


def test_no_http_only_internals():
    helper(2)


def test_swallowed():
    try:
        assert helper(2) == 4
    except Exception:
        pass
'''


def test_conftest_and_factory_from_real_repos(tmp_path, base_url):
    """conftest.py que importa la app y librerías ausentes, `TestClient(create_app())`, settings, fixtures con la base."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / "tests" / "test_factory.py").write_text(FACTORY)
    s = run_suite(base_url, tmp_path, "tests", 60)
    names = lambda xs: {x.rsplit("::", 1)[-1] for x in xs}  # noqa: E731
    assert s["passed"] == 1, s["log_tail"]
    assert names(s["failed"]) == {"test_real_failure"}, s
    assert names(x["nodeid"] for x in s["not_external"]) == {
        "test_fails_after_seeding", "test_reads_settings", "test_no_http_only_internals", "test_swallowed"}, s


def test_status_constants_without_fastapi():
    from cachai.pytest_http import _status_module

    status = _status_module("fastapi.status")
    assert status.HTTP_201_CREATED == 201
    assert status.HTTP_422_UNPROCESSABLE_ENTITY == 422
    assert status.HTTP_418_IM_A_TEAPOT == 418
    assert status.WS_1008_POLICY_VIOLATION == 1008
