from contraprueba.gate import Evidence, decide
from contraprueba.holdout import Attack, Predicate, split
from contraprueba.integrity import classify


def test_classify_flags_hooks_config_and_tests():
    changes = [
        ("A", "conftest.py"),
        ("A", "app/_hook.pth"),
        ("M", "pyproject.toml"),
        ("M", "tests/test_invoices.py"),
        ("M", "app/invoices.py"),
    ]
    flagged = {f.path: f.reason for f in classify(changes)}
    assert flagged == {
        "conftest.py": "hook de evaluación",
        "app/_hook.pth": "hook de evaluación",
        "pyproject.toml": "configuración de pytest",
        "tests/test_invoices.py": "tests existentes modificados",
    }


def test_gate_is_all_or_nothing():
    assert decide(Evidence(base_tests_passed=True)).approved
    assert not decide(Evidence(base_tests_passed=False)).approved
    assert not decide(Evidence(base_tests_passed=True, holdout_attacks_succeeded=["authz-3"])).approved


def test_split_keeps_holdout_hidden_and_deterministic():
    ok = Predicate(status_in=(200,))
    attacks = [Attack(f"a{i}", "authz", {"method": "GET", "path": f"/x/{i}"}, ok) for i in range(10)]
    shown, held = split(attacks, seed=1)
    assert shown and held
    assert not {a.id for a in shown} & {a.id for a in held}
    assert split(attacks, seed=1) == (shown, held)
