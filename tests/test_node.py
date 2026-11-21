"""Soporte para Node (#29): configuración, integridad de jest, vitest y mocha, y el caso notas-express."""

import json
from pathlib import Path

import pytest

from cachai import sandbox
from cachai.case import ConfigError, load_case, load_target
from cachai.integrity import _pytest_section, classify

ROOT = Path(__file__).resolve().parent.parent
CASE = ROOT / "cases" / "notas-express"


def test_case_loads_with_jest_and_its_own_dockerfile():
    c = load_case(CASE)
    t = c.target()
    assert (t.tests_runner, t.app.dockerfile, t.app.port, t.attack_families) == ("jest", "Dockerfile", 3000, ("authz",))
    assert load_target(json.dumps(t.to_dict())) == t
    assert {p.kind for p in c.prs} == {"honest", "cheat", "half_patch", "correct_patch"}


def test_jest_needs_an_image_of_the_repo():
    with pytest.raises(ConfigError) as e:
        load_target(json.dumps({"version": 2, "app": {"module": "a:b"}, "tests_runner": "jest"}))
    assert e.value.field == "tests_runner"
    with pytest.raises(ConfigError):
        load_target(json.dumps({"version": 2, "app": {"dockerfile": "Dockerfile"}, "tests_runner": "mocha-magico"}))


@pytest.mark.parametrize("path", ["jest.config.js", "vitest.config.ts", ".mocharc.json", "apps/web/jest.config.cjs"])
def test_node_test_config_changes_block(path):
    (f,) = classify([("M", path)])
    assert f.severity == "block" and f.reason == "configuración de tests de Node"


def test_package_json_blocks_only_when_its_test_config_changes():
    base = json.dumps({"dependencies": {"express": "4"}, "jest": {"testEnvironment": "node"}, "scripts": {"test": "jest"}}).encode()
    deps = json.dumps({"dependencies": {"express": "5"}, "jest": {"testEnvironment": "node"}, "scripts": {"test": "jest"}}).encode()
    hook = json.dumps({"dependencies": {"express": "4"}, "jest": {"setupFilesAfterEnv": ["./x.js"]}, "scripts": {"test": "jest"}}).encode()
    assert _pytest_section("package.json", base) == _pytest_section("package.json", deps)
    assert _pytest_section("package.json", base) != _pytest_section("package.json", hook)


def test_tests_command_follows_the_runner():
    c = load_case(CASE)
    assert sandbox.tests_command(c, "tests")[0] == sandbox.JEST
    assert sandbox.tests_command(None, "tests")[:3] == ["python", "-m", "pytest"]
