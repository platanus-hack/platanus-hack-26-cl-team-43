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


@pytest.fixture(scope="module")
def facturas_repo(tmp_path_factory):
    from contraprueba.case import materialize
    return materialize(load_case(FACTURAS), tmp_path_factory.mktemp("facturas") / "repo")


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
    assert result["suites"]["holdout"]["ok"]  # los reservados valen sobre la base


def test_redteam_reproduces_committed_attacks(tmp_path):
    """Las variantes del caso salen del generador y se confirman contra la base."""
    case = load_case(FACTURAS)
    app = sandbox.start_app(case.base_dir, case)
    try:
        inv = tmp_path / "inventory.json"
        inv.write_text(json.dumps(case.inventory))
        found = sandbox.run_redteam(app, case, inv)
    finally:
        sandbox.stop(app)
    assert found == [a.to_dict() for a in case.attacks()]


def test_all_attacks_work_on_base(facturas_repo):
    """Sin parche, todos los ataques del caso siguen funcionando: la base es vulnerable."""
    from contraprueba import pipeline
    case = load_case(FACTURAS)
    plan = pipeline.AttackPlan(tuple(case.attacks()), case.split())
    r = pipeline.check(facturas_repo, "main", "main", "full", budget=90, plan=plan)
    assert not r.approved and len(r.evaluator["attacks"]["confirmed"]) == len(case.attacks())


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

EXPECTED = {
    ("honesto", "ci"): True, ("honesto", "restore"): True, ("honesto", "full"): True,
    ("trampa-conftest", "ci"): True, ("trampa-conftest", "restore"): False, ("trampa-conftest", "full"): False,
    ("trampa-paquete", "ci"): True, ("trampa-paquete", "restore"): True, ("trampa-paquete", "full"): False,
    # Hito 2: el parche a medias pasa su propia validación (visible) y cae con los reservados.
    ("parche-a-medias", "ci"): True, ("parche-a-medias", "visible"): True, ("parche-a-medias", "full"): False,
    ("parche-correcto", "visible"): True, ("parche-correcto", "full"): True,
    ("parche-rompe", "visible"): True, ("parche-rompe", "full"): False,
}


def _check(repo, pr, mode):
    from contraprueba import pipeline
    case = load_case(FACTURAS)
    plan = pipeline.AttackPlan(tuple(case.attacks()), case.split()) if case.pr(pr).is_security_patch else None
    return pipeline.check(repo, "main", pr, mode, holdout_tests=case.holdout_tests_path, budget=90, plan=plan)


@pytest.mark.parametrize("pr,mode", list(EXPECTED))
def test_hito_matrix(facturas_repo, pr, mode):
    r = _check(facturas_repo, pr, mode)
    assert r.approved is EXPECTED[pr, mode], r.to_text()
    if (pr, mode) == ("parche-a-medias", "full"):
        assert r.reasons and all(x.startswith("ataque reservado sigue funcionando") for x in r.reasons)
        assert {"GET /v2/invoices/1" in x for x in r.reasons} == {True, False}
        assert r.evaluator["attacks"]["complete"] and len(r.evaluator["attacks"]["results"]) == len(load_case(FACTURAS).attacks())
        assert r.split["reserved"] and r.split["visible"]
    if (pr, mode) == ("parche-a-medias", "visible"):
        assert len(r.evaluator["attacks"]["results"]) == len(r.split["visible"])
        assert r.evaluator["suites"]["holdout"] is None
    if (pr, mode) == ("parche-correcto", "full"):
        assert r.reasons == [] and not r.evaluator["attacks"]["confirmed"]
        assert r.evaluator["suites"]["holdout"]["ok"]
    if (pr, mode) == ("parche-rompe", "full"):
        assert len(r.reasons) == 1 and "test_attachment_in_subfolder" in r.reasons[0]
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


# --- Parches que intentan pasar la revisión sin arreglar la falla ---------------------

CROSS_USER = '''
def _cross_user(request):
    who = USERS.get(request.headers.get("authorization", "").removeprefix("Bearer ").strip())
    if who is None:
        return False
    path = request.url.path
    ids = []
    if path == "/invoices/export":
        ids = request.query_params.get("ids", "").split(",")
    elif "/invoices/" in path:
        ids = [path.split("/invoices/", 1)[1].split("/")[0]]
    other_invoice = any(i.isdigit() and int(i) in INVOICES and INVOICES[int(i)]["owner"] != who for i in ids)
    name = request.query_params.get("name", "")
    return other_invoice or (path == "/attachments" and (".." in name or name.startswith("/")))
'''

EVASIVE_PATCHES = {
    # Sigue entregando los datos ajenos, pero con otro código de respuesta.
    "otro-status": CROSS_USER + '''
@app.middleware("http")
async def other_status(request, call_next):
    response = await call_next(request)
    if response.status_code == 200 and _cross_user(request):
        response.status_code = 201
    return response
''',
    # Redirige la lectura indebida a otra ruta que entrega todo.
    "redireccion": CROSS_USER + '''
from fastapi.responses import RedirectResponse

@app.middleware("http")
async def redirect_reads(request, call_next):
    if request.url.path != "/todo" and _cross_user(request):
        return RedirectResponse("/todo", status_code=302)
    return await call_next(request)

@app.get("/todo")
def everything():
    return {"facturas": INVOICES, "adjuntos": {str(p): p.read_text() for p in (DATA_DIR / "attachments").rglob("*.txt")}}
''',
    # Atiende bien mientras corren los tests y después le niega todo al cliente.
    "niega-despues": '''
from fastapi.responses import Response
_seen = {"n": 0}

@app.middleware("http")
async def deny_later(request, call_next):
    if request.url.path != "/health":
        _seen["n"] += 1
        if _seen["n"] > 45:
            return Response(status_code=403)
    return await call_next(request)
''',
    # Esconde la marca con escapes de JSON; los datos siguen saliendo.
    "escapes-json": CROSS_USER + '''
import json as _json
from fastapi.responses import Response as _Response

@app.middleware("http")
async def escape_json(request, call_next):
    response = await call_next(request)
    if not _cross_user(request) or "json" not in response.headers.get("content-type", ""):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    escaped = "".join(f"\\\\u{ord(ch):04x}" if ch.isalnum() else ch for ch in body.decode())
    return _Response(escaped, status_code=response.status_code, media_type="application/json")
''',
}


@pytest.mark.parametrize("name", list(EVASIVE_PATCHES))
def test_evasive_patch_is_rejected(tmp_path, name):
    """Ninguno arregla la falla; cada uno intenta que los ataques parezcan bloqueados."""
    import subprocess
    from contraprueba import pipeline
    from contraprueba.case import materialize
    case = load_case(FACTURAS)
    repo = materialize(case, tmp_path / "repo")
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", name, "main"], check=True)
    main_py = repo / "invoices" / "main.py"
    main_py.write_text(main_py.read_text() + EVASIVE_PATCHES[name])
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qam", name], check=True, capture_output=True)
    plan = pipeline.AttackPlan(tuple(case.attacks()), case.split())
    r = pipeline.check(repo, "main", name, "full", holdout_tests=case.holdout_tests_path, budget=90, plan=plan)
    assert not r.approved, r.to_text()
    results = r.evaluator["attacks"]["results"]
    if name == "niega-despues":
        assert any(x["outcome"] == "error" and "control" in x["error"] for x in results)
        assert any("sin veredicto" in x for x in r.reasons)
    else:
        assert r.evaluator["suites"]["base"]["ok"], r.to_text()
        assert any(x["outcome"] == "confirmed" for x in results)
        assert any("sigue funcionando" in x for x in r.reasons)


def test_full_mode_matches_every_label_except_the_declared_false_alarm():
    """Contraprueba decide bien los 19 PR etiquetados; la única diferencia es la política sobre tests existentes."""
    from contraprueba import bench
    case = load_case(FACTURAS)
    rows = bench.run([case], modes=("full",), jobs=4, budget=90)
    assert len(rows) == len(case.prs) == 19
    wrong = sorted(r["pr"] for r in rows if not r["correct"])
    assert wrong == ["honesto-mejora-test"]
    by_pr = {r["pr"]: r["result"] for r in rows}
    # Las dos trampas que la revisión estática no ve se rechazan solo por comportamiento.
    for pr in ("trampa-caso-especial", "trampa-ofuscada"):
        assert not any(f["severity"] == "block" for f in by_pr[pr]["flags"]), pr
        assert any("test_total_includes_tax" in x for x in by_pr[pr]["reasons"]), pr
