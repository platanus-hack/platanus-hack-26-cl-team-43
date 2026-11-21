"""`cachai init` (#7): detección, borrador de inventario, configuración y acceptación sin sandbox."""

import json
import subprocess
from pathlib import Path

import pytest

from cachai import cli, init
from cachai.case import CONFIG_FILE, load_case, load_target, materialize
from cachai.gate import Evidence
from cachai.holdout import Attack, Predicate

ROOT = Path(__file__).resolve().parent.parent
FACTURAS = ROOT / "cases" / "facturas"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


# --- detección ---------------------------------------------------------------

def test_detects_the_asgi_object_health_and_tests_dir():
    det = init.detect(FACTURAS / "base")
    assert det.module == "invoices.main:app"
    assert det.health == "/health"
    assert det.tests_dir == "tests"
    assert det.dockerfile is None and det.compose is None


def test_module_of_strips_init_and_skips_tests(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("app = FastAPI()\n")
    assert init.module_of(tmp_path, tmp_path / "pkg" / "__init__.py") == "pkg"
    assert [c.module for c in init.detect_asgi(tmp_path)] == []


def test_asgi_candidates_prefer_fastapi_then_app(tmp_path):
    (tmp_path / "a.py").write_text("api = make_app()\n")
    (tmp_path / "b.py").write_text("app = FastAPI()\n")
    assert [c.module for c in init.detect_asgi(tmp_path)] == ["b:app", "a:api"]


# --- usuarios de prueba --------------------------------------------------------

def test_parse_user_and_header():
    assert init.parse_user("alice=Authorization: Bearer tok") == ("alice", "Authorization: Bearer tok")
    with pytest.raises(init.InitError):
        init.parse_user("Alice=Authorization: Bearer tok")  # el nombre va en minúsculas
    with pytest.raises(init.InitError):
        init.parse_header("sin dos puntos")
    assert init.parse_header("X-Key:  valor ") == ("X-Key", "valor")


def test_login_header_accepts_a_header_or_a_bare_token():
    class Proc:
        returncode = 0
        stdout = "ruido\nAuthorization: Bearer abc\n"

    def run(*a, **k):
        return Proc()

    assert init.login_header("cmd", run) == ("Authorization", "Bearer abc")
    Proc.stdout = "tok-suelto\n"
    assert init.login_header("cmd", run) == ("Authorization", "Bearer tok-suelto")
    Proc.returncode = 1
    with pytest.raises(init.InitError):
        init.login_header("cmd", run)


# --- borrador de inventario ----------------------------------------------------

USERS = {"alice": {"Authorization": "Bearer a"}, "bob": {"Authorization": "Bearer b"}}


def test_draft_records_finds_owner_and_markers_only_the_owner_sees():
    samples = {
        "/invoices": {
            "alice": [{"id": 1, "number": "FAC-A-001", "owner": "alice"}],
            "bob": [{"id": 3, "number": "FAC-B-001", "owner": "bob"}],
        }
    }
    records = init.draft_records(samples, USERS)
    assert records == [
        {"id": "1", "owner": "alice", "markers": ["FAC-A-001"]},
        {"id": "3", "owner": "bob", "markers": ["FAC-B-001"]},
    ]


def test_draft_records_skips_objects_seen_by_everyone():
    samples = {"/items": {"alice": [{"id": "1", "name": "compartido"}], "bob": [{"id": "1", "name": "compartido"}]}}
    assert init.draft_records(samples, USERS) == []


def test_draft_inventory_needs_two_users_and_something_owned(tmp_path):
    samples = {"/invoices": {"alice": [{"id": 1, "number": "FAC-A-001"}], "bob": [{"id": 3, "number": "FAC-B-001"}]}}
    inv = init.draft_inventory(FACTURAS / "base", USERS, samples)
    assert inv["users"] == USERS and inv["records"][0]["owner"] == "alice"
    assert "files" in inv  # la carpeta data/attachments/alice y /bob del caso
    assert init.draft_inventory(FACTURAS / "base", {"alice": USERS["alice"]}, samples) is None
    assert init.draft_inventory(tmp_path, USERS, {}) is None


def test_default_families_follow_the_inventory():
    assert init.default_families({"users": USERS, "records": [{"id": "1", "owner": "alice", "markers": ["xxxx"]}]}) == [
        "authz", "authn", "sqli", "writes"]
    assert init.default_families({"users": USERS, "files": [{"path": "a.txt"}], "files_root": "/app/data"}) == [
        "files", "authn"]
    assert init.default_families(None) == []


def test_draft_files_uses_a_folder_per_user(tmp_path):
    for user, text in (("alice", "contrato de alice"), ("bob", "orden de bob")):
        d = tmp_path / "data" / "attachments" / user
        d.mkdir(parents=True)
        (d / "doc.txt").write_text(text + "\n")
    files, root = init.draft_files(tmp_path, USERS, set())
    assert root == "/app/data/attachments"
    assert {f["owner"] for f in files} == {"alice", "bob"}


# --- escritura -----------------------------------------------------------------

def test_workflow_text_grants_the_permissions_the_action_needs():
    text = init.workflow_text()
    assert "pull-requests: write" in text and "checks: write" in text
    assert "CACHAI_HIDDEN_KEY" in text


def _strict_yaml(text):
    """Carga YAML rechazando claves repetidas, como hace GitHub Actions."""
    import yaml

    class Strict(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
        assert len(keys) == len(set(keys)), f"claves repetidas: {keys}"
        return loader.construct_mapping(node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    return yaml.load(text, Loader=Strict)


@pytest.mark.parametrize("key_env", [init.DEFAULT_KEY_ENV, "OTRA_CLAVE"])
def test_generated_workflow_is_valid_yaml_without_repeated_keys(key_env):
    wf = _strict_yaml(init.workflow_text(key_env))
    assert wf["permissions"] == {"contents": "read", "pull-requests": "write", "checks": "write"}


def test_workflow_text_adds_only_missing_permissions(tmp_path):
    example = tmp_path / "workflow.yml"
    example.write_text("name: x\non: pull_request\npermissions:\n  contents: read\n\njobs:\n  cachai:\n    runs-on: ubuntu-latest\n"
                       "    steps:\n      - uses: x\n        with:\n          hidden-key: ${{ secrets.CACHAI_HIDDEN_KEY }}\n")
    wf = _strict_yaml(init.workflow_text(example=example))
    assert wf["permissions"] == {"contents": "read", "pull-requests": "write", "checks": "write"}


def test_validate_config_uses_the_published_loader():
    target = init.validate_config(load_case(FACTURAS).target().to_dict())
    assert target.tests_dir == "tests"
    import jsonschema
    with pytest.raises((ValueError, jsonschema.ValidationError)):
        init.validate_config({"app": {"shell": "sh"}})


def test_init_without_sandbox_writes_config_and_workflow(tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "main.py").write_text("from fastapi import FastAPI\n\napp = FastAPI()\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    code = cli.main(["init", "--repo", str(repo), "--yes", "--no-sandbox"])
    assert code == 0
    cfg = json.loads((repo / CONFIG_FILE).read_text())
    assert cfg["app"] == {"module": "app.main:app", "port": 8000, "health": "/openapi.json"}
    assert cfg["tests_dir"] == "tests"
    assert (repo / init.WORKFLOW_FILE).is_file()
    assert "no hay inventario" in capsys.readouterr().out


def test_init_refuses_to_overwrite_without_force(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / CONFIG_FILE).write_text("{}")
    code = cli.main(["init", "--repo", str(repo), "--yes", "--no-sandbox"])
    assert code == 2


# --- aceptación: el paquete sellado por init no culpa al PR de las fallas de la base ---

def test_materialize_can_use_an_already_initialized_base(tmp_path):
    case = load_case(FACTURAS)
    base = tmp_path / "configurada"
    base.mkdir()
    (base / "app.py").write_text("x = 1\n")
    (base / CONFIG_FILE).write_text(json.dumps({"app": {"module": "app:app"}}))
    repo = materialize(case, tmp_path / "repo", base=base)
    assert json.loads(git(repo, "show", "main:.cachai.json")) == {"app": {"module": "app:app"}}
    assert "parche-correcto" in git(repo, "branch", "--list")


def test_excuse_existing_does_not_excuse_a_pr_that_tries_to_fix():
    ok = Predicate(status_in=(200,))
    a = Attack("authz-1", "authz", {"method": "GET", "path": "/x/1"}, ok)
    b = Attack("authz-2", "authz", {"method": "GET", "path": "/x/2"}, ok)
    from cachai.holdout import Split
    from cachai.pipeline import AttackPlan

    plan = AttackPlan((a, b), Split(0, 0.5, ("authz-1",), ("authz-2",)))
    ev = Evidence(False, holdout_attacks_succeeded=[plan.describe("authz-2")])
    # El PR cierra authz-1 (visible) y deja authz-2: es un intento de arreglo, no se excusa nada.
    assert init_excuse(ev, plan, ("authz-1", "authz-2"), ["authz-2"], {"authz-1", "authz-2"}) == []


def init_excuse(ev, plan, ids, confirmed, base_open):
    from cachai.pipeline import excuse_existing
    return excuse_existing(ev, plan, ids, confirmed, base_open)


def test_excuse_existing_drops_only_failures_already_in_the_base():
    ok = Predicate(status_in=(200,))
    a = Attack("authz-1", "authz", {"method": "GET", "path": "/x/1"}, ok)
    b = Attack("authz-2", "authz", {"method": "GET", "path": "/x/2"}, ok)
    from cachai.holdout import Split
    from cachai.pipeline import AttackPlan

    plan = AttackPlan((a, b), Split(0, 0.5, ("authz-1",), ("authz-2",)))
    before = plan.describe("authz-2")
    ev = Evidence(False, holdout_attacks_succeeded=[before], holdout_attacks_public=[("authz", "GET /x/2")])
    excused = init_excuse(ev, plan, ("authz-1", "authz-2"), ["authz-2"], {"authz-2"})
    assert excused == ["authz-2"]
    assert ev.holdout_attacks_succeeded == [] and ev.holdout_attacks_public == []
