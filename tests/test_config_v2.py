"""Configuración v2 de `.cachai.json`: esquema publicado, errores por campo y lectura de la v1."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from cachai import cli, integrity
from cachai.case import (
    APP_KEYS, ATTACKS_KEYS, CONFIG_FILE, CONFIG_KEYS, HIDDEN_KEYS, ConfigError, load_case, load_target, materialize,
)
from cachai.holdout import FAMILIES
from cachai.redteam import INVENTORY_KEYS

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schema" / "cachai.schema.json").read_text())
FACTURAS = ROOT / "cases" / "facturas"

V2 = {
    "version": 2,
    "app": {"module": "invoices.main:app", "port": 8000, "health": "/health"},
    "tests_dir": "tests",
    "inventory": {
        "users": {"alice": {"Authorization": "Bearer a"}, "bob": {"Authorization": "Bearer b"}},
        "records": [{"id": "1", "owner": "alice", "markers": ["FAC-A-001"]}],
        "files": [{"path": "alice/c.txt", "owner": "alice", "markers": ["Contrato"]}],
        "files_root": "/app/data",
    },
    "attacks": {"families": ["authz", "files"]},
    "hidden": {"package": ".cachai/reservado.age", "key_env": "CACHAI_HIDDEN_KEY"},
}


def with_(path: str, value=None, delete: bool = False) -> str:
    d = copy.deepcopy(V2)
    *parents, last = path.split(".")
    node = d
    for p in parents:
        node = node[p]
    if delete:
        del node[last]
    else:
        node[last] = value
    return json.dumps(d)


def test_v2_loads_and_roundtrips():
    t = load_target(json.dumps(V2))
    assert t.app.module == "invoices.main:app"
    assert t.attack_families == ("authz", "files")
    assert t.hidden.key_env == "CACHAI_HIDDEN_KEY"
    assert t.to_dict() == V2
    assert load_target(json.dumps(t.to_dict())) == t


def test_v1_still_loads():
    t = load_target('{"app": {"module": "a.b:app"}, "tests_dir": "pruebas"}')
    assert (t.app.module, t.tests_dir, t.inventory, t.attack_families, t.hidden) == ("a.b:app", "pruebas", None, (), None)


@pytest.mark.parametrize("data, field", [
    (with_("extra", 1), "extra"),
    (with_("app.shell", "sh"), "app.shell"),
    (with_("attacks.payloads", []), "attacks.payloads"),
    (with_("hidden.material", "x"), "hidden.material"),
    ('{"app": {"module": "a:b"}, "inventory": {}}', "inventory"),  # v1 no acepta campos de la v2
])
def test_unknown_field_is_rejected_naming_it(data, field):
    with pytest.raises(ConfigError) as e:
        load_target(data)
    assert e.value.field == field
    assert f"«{field}»" in str(e.value) and "desconocido" in str(e.value)


@pytest.mark.parametrize("data, field", [
    (with_("version", 3), "version"),
    ('{"version": 1, "app": {"module": "a:b"}}', "version"),
    (with_("app", None, delete=True), "app"),
    (with_("app.module", "no es un módulo"), "app"),
    (with_("tests_dir", "../fuera"), "tests_dir"),
    (with_("attacks.families", ["rce"]), "attacks.families"),
    (with_("attacks.families", ["authz", "authz"]), "attacks.families"),
    (with_("inventory", None, delete=True), "inventory"),
    (with_("inventory.users", {"solo": {"A": "b"}}), "inventory"),
    (with_("hidden.key_env", "minúsculas"), "hidden.key_env"),
    (with_("hidden.package", "../reservado.age"), "hidden.package"),
    (with_("hidden.package", ""), "hidden.package"),
    ("{no es json", ""),
    ("[]", ""),
])
def test_invalid_values_name_the_field(data, field):
    with pytest.raises(ConfigError) as e:
        load_target(data)
    assert e.value.field == field
    assert str(e.value).startswith(CONFIG_FILE)


def test_schema_matches_the_loader():
    props = SCHEMA["properties"]
    assert set(props) == CONFIG_KEYS
    assert SCHEMA["additionalProperties"] is False
    assert set(props["app"]["properties"]) == APP_KEYS
    assert set(props["attacks"]["properties"]) == ATTACKS_KEYS
    assert set(props["hidden"]["properties"]) == HIDDEN_KEYS
    assert set(props["inventory"]["properties"]) == INVENTORY_KEYS
    assert tuple(props["attacks"]["properties"]["families"]["items"]["enum"]) == FAMILIES


def test_cases_materialize_a_v2_config(tmp_path):
    case = load_case(FACTURAS)
    repo = materialize(case, tmp_path / "repo")
    d = json.loads((repo / CONFIG_FILE).read_text())
    # facturas declara sus familias en case.json: authn no tiene ataques en la base, pero se caza en el PR.
    assert d["version"] == 2 and d["attacks"]["families"] == ["authz", "files", "authn", "writes"]
    assert load_target(json.dumps(d)) == case.target()


def test_case_families_default_to_the_attacks_and_are_validated(tmp_path):
    import shutil
    root = tmp_path / "caso"
    shutil.copytree(FACTURAS, root)
    d = json.loads((root / "case.json").read_text())
    del d["families"]
    (root / "case.json").write_text(json.dumps(d))
    assert load_case(root).target().attack_families == ("authz", "files")
    for bad in (["rce"], ["authz", "authz"], "authz"):
        d["families"] = bad
        (root / "case.json").write_text(json.dumps(d))
        with pytest.raises(ValueError):
            load_case(root)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                   check=True, capture_output=True)


def _repo(tmp_path, config: str) -> Path:
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    (r / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    (r / CONFIG_FILE).write_text(config)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def test_check_rejects_unknown_field_with_a_clear_message(tmp_path, capsys):
    repo = _repo(tmp_path, with_("app.shell", "sh"))
    assert cli.main(["check", "--repo", str(repo), "--pr", "main"]) == 2
    err = capsys.readouterr().err
    assert "«app.shell»" in err and "campo desconocido" in err


def test_pr_that_changes_a_v2_config_is_blocked(tmp_path):
    repo = _repo(tmp_path, json.dumps(V2))
    _git(repo, "checkout", "-q", "-b", "pr")
    (repo / CONFIG_FILE).write_text(with_("attacks.families", ["files"]))  # apaga los ataques de autorización
    _git(repo, "commit", "-q", "-am", "pr")
    from cachai import gitio
    flags = integrity.scan(repo, gitio.resolve(repo, "main"), gitio.resolve(repo, "pr"))
    assert [(f.path, f.severity) for f in flags] == [(CONFIG_FILE, "block")]


def test_check_reads_a_v2_config_from_the_base(tmp_path):
    from cachai import gitio, pipeline
    repo = _repo(tmp_path, json.dumps(V2))
    assert pipeline.target_from_base(repo, gitio.resolve(repo, "main")) == load_target(json.dumps(V2))


def test_examples_validate_against_the_published_schema():
    import jsonschema
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    v = jsonschema.Draft202012Validator(SCHEMA)
    v.validate(V2)
    v.validate(load_case(FACTURAS).target().to_dict())
    for bad in (with_("app.shell", "sh"), with_("attacks.families", ["rce"]), with_("hidden.key_env", "x")):
        assert not v.is_valid(json.loads(bad))
