"""Quemar y rotar reservados (#10): el grupo revelado pasa a visible, el reporte lo firma y la rotación repone."""

import json
from pathlib import Path

import pytest

from cachai import cli, hidden, pipeline
from cachai.case import load_case
from cachai.holdout import Attack, Split, reserved_groups

ROOT = Path(__file__).resolve().parent.parent
FACTURAS = ROOT / "cases" / "facturas"
KEY = "k" * 64


@pytest.fixture(scope="module")
def case():
    return load_case(FACTURAS)


@pytest.fixture(scope="module")
def sealed(case):
    return hidden.seal(KEY, FACTURAS / case.attacks_file, case.holdout_tests_path, case.split_shown, case.split_seed)


def _reserved_group(case):
    groups = reserved_groups(case.attacks(), case.split())
    return next(group for gs in groups.values() for group in sorted(gs))


def test_burn_moves_a_reserved_group_to_visible(sealed, case):
    family, group = _reserved_group(case)
    new, result = hidden.burn(sealed, KEY, {(family, group)})
    assert new is not None
    assert result.burned == ((family, group),)
    m = hidden.open_package(new, KEY, _tmp_dir())
    moved = {a.id for a in m.attacks if a.group_key == (family, group)}
    assert moved <= set(m.split.visible) and not (moved & set(m.split.reserved))
    assert moved <= set(m.split.burned)
    assert result.remaining[family] == len(reserved_groups(case.attacks(), case.split())[family]) - 1


def _tmp_dir():
    """Carpeta nueva para `open_package`, que exige que no exista."""
    import tempfile
    return Path(tempfile.mkdtemp()) / "paquete"


def test_burn_is_idempotent(sealed, case):
    family, group = _reserved_group(case)
    new, _ = hidden.burn(sealed, KEY, {(family, group)})
    again, result = hidden.burn(new, KEY, {(family, group)})
    assert again is None  # nada que mover
    assert result.already == ((family, group),)


def test_burn_unknown_group_changes_nothing(sealed):
    new, result = hidden.burn(sealed, KEY, {("authz", "no-existe")})
    assert new is None and result.unknown == ("authz:no-existe",)


def test_report_marks_revealed_groups_with_opaque_tokens(case):
    split = case.split()
    plan = pipeline.AttackPlan(tuple(case.attacks()), split)
    revealed = sorted(split.reserved)[0]
    status, notes = pipeline.reserve_status(plan, [revealed], "a" * 40, KEY)
    assert status["revealed"] == 1 and status["burn"]
    assert all(len(t) == hidden.BURN_TOKEN_LEN for t in status["burn"])
    assert any("cachai hidden burn" in n for n in notes)


def test_tokens_in_the_report_burn_the_right_group(sealed, case):
    split = case.split()
    plan = pipeline.AttackPlan(tuple(case.attacks()), split)
    revealed = sorted(split.reserved)[0]
    status, _ = pipeline.reserve_status(plan, [revealed], "a" * 40, KEY)
    report = {"base": "a" * 40, "reserve": status}
    groups, unknown = hidden.groups_from_report(sealed, KEY, report)
    assert not unknown and len(groups) == 1
    group = next(iter(groups))
    assert revealed in {a.id for a in case.attacks() if a.group_key == group}


def test_token_from_another_base_or_package_is_ignored(sealed, case):
    split = case.split()
    plan = pipeline.AttackPlan(tuple(case.attacks()), split)
    status, _ = pipeline.reserve_status(plan, [sorted(split.reserved)[0]], "a" * 40, KEY)
    groups, unknown = hidden.groups_from_report(sealed, KEY, {"base": "b" * 40, "reserve": status})
    assert groups == set() and unknown == status["burn"]


def test_cli_burn_with_a_report(tmp_path, monkeypatch, capsys):
    case = load_case(FACTURAS)
    pkg = tmp_path / "p.cachai"
    pkg.write_bytes(hidden.seal(KEY, FACTURAS / case.attacks_file, None, 0.3, 0))
    plan = pipeline.AttackPlan(tuple(case.attacks()), hidden.open_package(pkg.read_bytes(), KEY, tmp_path / "abierto").split)
    split = plan.split
    revealed = sorted(split.reserved)[0]
    status, _ = pipeline.reserve_status(plan, [revealed], "a" * 40, KEY)
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"base": "a" * 40, "reserve": status}))
    monkeypatch.setenv("CACHAI_HIDDEN_KEY", KEY)
    assert cli.main(["hidden", "burn", str(pkg), "--report", str(report)]) == 0
    out = capsys.readouterr().out
    assert "quemado:" in out and "grupos reservados que quedan" in out


def test_report_schema_accepts_the_reserve_block(case):
    import jsonschema
    schema = json.loads((ROOT / "schema" / "report.schema.json").read_text())
    plan = pipeline.AttackPlan(tuple(case.attacks()), case.split())
    revealed = sorted(plan.split.reserved)[0]
    status, _ = pipeline.reserve_status(plan, [revealed], "a" * 40, KEY)
    report = {"schema_version": 2, "mode": "full", "base": "a" * 40, "pr": "b" * 40, "approved": False,
              "outcome": "rejected", "checked": [], "reasons": [{"kind": "attack_hidden", "text": "x"}],
              "notes": [], "reserve": status,
              "public": {"summary": "x", "markdown": "x"}}
    jsonschema.Draft202012Validator(schema).validate(report)


def test_a_plan_without_reserved_is_valid_after_burning_everything():
    ok = Attack("a", "authz", {"method": "GET", "path": "/x"}, __import__("cachai.holdout", fromlist=["Predicate"]).Predicate(status_in=(200,)))
    plan = pipeline.AttackPlan((ok,), Split(0, 0.5, ("a",), (), ("a",)))
    assert plan.split.reserved == ()


def test_rotation_adds_new_groups_only_for_burned_families(case, sealed):
    family, group = _reserved_group(case)
    new, _ = hidden.burn(sealed, KEY, {(family, group)})
    found = [a for a in case.attacks() if a.family == family]
    rotated, result = hidden.rotate(new, KEY, found)
    assert rotated is None  # todas las variantes conocidas ya están en el paquete
    assert result.notes and "no tiene variantes nuevas" in result.notes[0]


def test_rotation_adds_attacks_that_are_not_in_the_package(case, sealed):
    family, group = _reserved_group(case)
    new, _ = hidden.burn(sealed, KEY, {(family, group)})
    model = next(a for a in case.attacks() if a.family == family)
    invented = Attack("nuevo-1", family, {"method": "GET", "path": "/nuevo"}, model.succeeded_if)
    rotated, result = hidden.rotate(new, KEY, [invented])
    assert rotated is not None and result.new_groups[family] == 1
    m = hidden.open_package(rotated, KEY, _tmp_dir())
    assert "nuevo-1" in {a.id for a in m.attacks} and "nuevo-1" in m.split.reserved


def test_rotation_candidates_include_guarded_variants():
    from cachai.redteam import Inventory, candidates

    inv = Inventory.from_dict({
        "users": {"alice": {"Authorization": "Bearer a"}, "bob": {"Authorization": "Bearer b"}},
        "files": [{"path": "alice/doc.txt", "owner": "alice", "markers": ["SECRETO-A"]}],
        "files_root": "/app/data",
    })
    routes = [("/files", [], ["name"])]
    normal = candidates(inv, routes, families=("files",))
    rotated = candidates(inv, routes, families=("files",), rotation=True)
    assert {c.id for c in normal} < {c.id for c in rotated}
    assert any(c.request["path"].startswith("/files/?") for c in rotated)
    assert any("%2E%2E" in c.request["path"] for c in rotated)
