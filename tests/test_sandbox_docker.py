"""Integración con Docker: app y evaluador en contenedores separados.

Lentos (construyen imágenes). Corren solo con CP_DOCKER=1.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from contraprueba import sandbox
from contraprueba.case import load_case

pytestmark = pytest.mark.skipif(os.environ.get("CP_DOCKER") != "1", reason="requiere CP_DOCKER=1")

FACTURAS = Path(__file__).resolve().parent.parent / "cases" / "facturas"
REQS = (FACTURAS / "base" / "requirements.txt").read_text()


@pytest.fixture(scope="module", autouse=True)
def evaluator_image():
    sandbox.build_evaluator_image()


def evaluate(case_dir: Path, tmp_path: Path, budget: float = 60):
    case = load_case(case_dir)
    app = sandbox.start_app(case.base_dir, case)
    try:
        workdir = sandbox.prepare_eval_workdir(case.base_dir, case.tests_dir, tmp_path / "work")
        return sandbox.run_evaluator(app, case, workdir, case.holdout_tests_path, budget)
    finally:
        sandbox.stop(app)


def make_case(root: Path, app_code: str, tests: dict[str, str], module="api.server:application",
              port=9123, health="/ping", tests_dir="checks", holdout: dict[str, str] | None = None) -> Path:
    base = root / "base"
    (base / "api").mkdir(parents=True)
    (base / "api" / "__init__.py").write_text("")
    (base / "api" / "server.py").write_text(app_code)
    (base / "requirements.txt").write_text(REQS)
    (base / tests_dir).mkdir()
    for name, body in tests.items():
        (base / tests_dir / name).write_text(body)
    manifest = {"id": "t", "app": {"module": module, "port": port, "health": health}, "tests_dir": tests_dir}
    if holdout:
        (root / "reservados").mkdir()
        for name, body in holdout.items():
            (root / "reservados" / name).write_text(body)
        manifest["holdout_tests"] = "reservados"
    (root / "case.json").write_text(json.dumps(manifest))
    return root


APP = """
import time
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
application = FastAPI()

@application.get("/ping")
def ping():
    return {"pong": True}

@application.get("/stream")
def stream():
    def forever():
        while True:
            yield b"."
            time.sleep(1)
    return StreamingResponse(forever())
"""

CONFTEST = """
import os, httpx, pytest

@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=os.environ["BASE_URL"], timeout=10) as c:
        yield c
"""


def test_facturas_base_is_green(tmp_path):
    code, result = evaluate(FACTURAS, tmp_path)
    assert code == 0 and result["status"] == "ok"
    assert result["suites"]["base"]["passed"] == 12


def test_failing_suite_exits_nonzero(tmp_path):
    case = tmp_path / "case"
    shutil.copytree(FACTURAS, case)
    (case / "base" / "tests" / "test_invoices.py").write_text("def test_intentional_failure():\n    assert False\n")
    code, result = evaluate(case, tmp_path)
    assert code == 1 and result["status"] == "failed"
    assert result["suites"]["base"]["failed"]


def test_non_default_port_health_and_tests_dir_with_holdout(tmp_path):
    case = make_case(
        tmp_path / "case", APP,
        {"conftest.py": CONFTEST, "test_ping.py": "def test_ping(client):\n    assert client.get('/ping').json() == {'pong': True}\n"},
        holdout={"conftest.py": CONFTEST, "test_r.py": "def test_r(client):\n    assert client.get('/ping').status_code == 200\n"},
    )
    code, result = evaluate(case, tmp_path)
    assert code == 0, result
    assert result["suites"]["base"]["passed"] == 1
    assert result["suites"]["holdout"]["passed"] == 1


def test_endless_response_is_incomplete_not_hung(tmp_path):
    case = make_case(
        tmp_path / "case", APP,
        {"conftest.py": CONFTEST, "test_stream.py": "def test_stream(client):\n    assert client.get('/stream').status_code == 200\n"},
    )
    code, result = evaluate(case, tmp_path, budget=15)
    assert code == 2 and result["status"] == "incomplete"
    assert result["suites"]["base"]["timed_out"]


def test_app_that_never_starts_is_incomplete(tmp_path):
    case = make_case(tmp_path / "case", "raise SystemExit('roto')\n", {"conftest.py": CONFTEST, "test_a.py": "def test_a(client):\n    pass\n"})
    code, result = evaluate(case, tmp_path, budget=10)
    assert code == 2 and result["status"] == "incomplete" and result["errors"]
