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


# --- Fase 1: los tres modos sobre los PR del caso facturas -------------------------

@pytest.fixture(scope="module")
def facturas_repo(tmp_path_factory):
    from contraprueba.case import materialize
    return materialize(load_case(FACTURAS), tmp_path_factory.mktemp("facturas") / "repo")


EXPECTED = {
    ("honesto", "ci"): True, ("honesto", "restore"): True, ("honesto", "full"): True,
    ("trampa-conftest", "ci"): True, ("trampa-conftest", "restore"): False, ("trampa-conftest", "full"): False,
    ("trampa-paquete", "ci"): True, ("trampa-paquete", "restore"): True, ("trampa-paquete", "full"): False,
}


@pytest.mark.parametrize("pr,mode", list(EXPECTED))
def test_hito_1_matrix(facturas_repo, pr, mode):
    from contraprueba import pipeline
    r = pipeline.check(facturas_repo, "main", pr, mode, budget=90)
    assert r.approved is EXPECTED[pr, mode], r.to_text()
    if (pr, mode) == ("trampa-paquete", "full"):
        text = "\n".join(r.reasons)
        assert "tests/test_invoices.py:48 test_total_includes_tax" in text
        assert "invoices/_telemetry.py:9" in text
        assert r.evaluator["status"] == "failed"
    if (pr, mode) == ("honesto", "full"):
        assert r.reasons == [] and any("test_discount.py" in n for n in r.notes)


def test_stale_branch_is_rejected(tmp_path):
    """Si la base avanzó, el PR no se evalúa contra tests que no conoce."""
    import subprocess
    from contraprueba import pipeline
    from contraprueba.case import materialize
    repo = materialize(load_case(FACTURAS), tmp_path / "repo")
    (repo / "NOTAS.md").write_text("avance de main\n")
    for args in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm", "avanza"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    r = pipeline.check(repo, "main", "honesto", "full")
    assert not r.approved and "actualizarla" in r.reasons[0]


def test_pr_that_breaks_the_build_is_incomplete(tmp_path):
    import subprocess
    from contraprueba import pipeline
    from contraprueba.case import materialize
    repo = materialize(load_case(FACTURAS), tmp_path / "repo")
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "roto", "main"], check=True)
    (repo / "requirements.txt").write_text("paquete-que-no-existe-contraprueba==9.9.9\n")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qam", "roto"], check=True, capture_output=True)
    r = pipeline.check(repo, "main", "roto", "full")
    assert not r.approved and "incompleta" in r.reasons[0]


def test_partially_skipped_holdout_rejects_honest_pr(facturas_repo, tmp_path):
    """El rechazo del evaluador no se pierde al armar la evidencia."""
    from contraprueba import pipeline
    holdout = tmp_path / "reservados"
    holdout.mkdir()
    (holdout / "conftest.py").write_text(CONFTEST)
    (holdout / "test_reserved.py").write_text(
        "import pytest\n\ndef test_a(client):\n    assert client.get('/health').status_code == 200\n\n"
        "@pytest.mark.skip(reason='no')\ndef test_b(client):\n    assert False\n"
    )
    r = pipeline.check(facturas_repo, "main", "honesto", "full", holdout_tests=holdout, budget=90)
    assert r.evaluator["status"] == "failed"
    assert not r.approved and any("omitidos" in x for x in r.reasons), r.to_text()


def test_holdout_that_passes_keeps_honest_pr_approved(facturas_repo, tmp_path):
    from contraprueba import pipeline
    holdout = tmp_path / "reservados"
    holdout.mkdir()
    (holdout / "conftest.py").write_text(CONFTEST)
    (holdout / "test_reserved.py").write_text("def test_a(client):\n    assert client.get('/health').status_code == 200\n")
    r = pipeline.check(facturas_repo, "main", "honesto", "full", holdout_tests=holdout, budget=90)
    assert r.approved, r.to_text()


def test_git_replace_cannot_launder_a_bad_commit(tmp_path):
    """Con un reemplazo local hacia el commit honesto, el hash malo se sigue rechazando."""
    import subprocess
    from contraprueba import gitio, pipeline
    from contraprueba.case import materialize
    repo = materialize(load_case(FACTURAS), tmp_path / "repo")
    bad, good = gitio.resolve(repo, "trampa-paquete"), gitio.resolve(repo, "honesto")
    subprocess.run(["git", "-C", str(repo), "replace", bad, good], check=True, capture_output=True)
    r = pipeline.check(repo, "main", "trampa-paquete", "full", budget=90)
    assert r.pr == bad and not r.approved
    assert any("test_total_includes_tax" in x for x in r.reasons)
