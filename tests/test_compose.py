"""Compose de la base interpretado por Cachai: subconjunto permitido y rechazos."""

from pathlib import Path

import pytest

from cachai import compose
from cachai.case import load_target
from cachai.integrity import classify

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "postgres-notas"


def test_example_compose_parses_app_and_db():
    plan = compose.parse((EXAMPLE / "docker-compose.yml").read_text(), "api")
    assert plan.app.build_context == "." and dict(plan.app.environment)["DATABASE_URL"].startswith("postgresql://")
    (db,) = plan.services
    assert db.image == "postgres:16-alpine" and db.health_cmd == "pg_isready -U notas -d notas"


def test_example_config_loads():
    t = load_target((EXAMPLE / ".cachai.json").read_text())
    assert t.app.compose.service == "api" and t.app.module is None


BASE = "services:\n  api:\n    build: .\n  db:\n    image: postgres:16\n"


@pytest.mark.parametrize("extra, why", [
    ("    privileged: true\n", "privileged"),
    ("    network_mode: host\n", "network_mode"),
    ("    cap_add: [SYS_ADMIN]\n", "cap_add"),
    ("    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n", "socket"),
    ("    volumes:\n      - ./datos:/data\n", "host"),
    ("    volumes:\n      - type: bind\n        source: /etc\n        target: /x\n", "host"),
    ("    environment:\n      - SECRETO\n", "valor"),
])
def test_dangerous_service_options_are_rejected(extra, why):
    with pytest.raises(compose.ComposeError, match=why):
        compose.parse(BASE + extra, "api")


def test_named_volumes_and_ports_are_ignored():
    plan = compose.parse(BASE + "    ports: ['5432:5432']\n    volumes: ['pgdata:/var/lib/postgresql/data']\nvolumes:\n  pgdata:\n", "api")
    assert plan.services[0].name == "db"


def test_only_the_app_is_built_and_it_must_exist():
    with pytest.raises(compose.ComposeError, match="solo la app"):
        compose.parse("services:\n  api:\n    build: .\n  worker:\n    build: ./w\n", "api")
    with pytest.raises(compose.ComposeError, match="no tiene el servicio"):
        compose.parse(BASE, "web")
    with pytest.raises(compose.ComposeError, match="build"):
        compose.parse(BASE, "db")


def test_build_context_cannot_escape_the_repo():
    with pytest.raises(ValueError):
        compose.parse("services:\n  api:\n    build: ../fuera\n", "api")


def test_services_start_in_dependency_order():
    plan = compose.parse("services:\n  api:\n    build: .\n  cache:\n    image: redis\n    depends_on: [db]\n  db:\n    image: postgres\n", "api")
    assert [s.name for s in plan.services] == ["db", "cache"]


def test_pr_that_changes_the_compose_is_blocked_and_dockerfile_is_info():
    flags = classify([("M", "docker-compose.yml"), ("M", "Dockerfile")], eval_files=("docker-compose.yml",), app_files=("Dockerfile",))
    assert [(f.path, f.severity) for f in flags] == [("docker-compose.yml", "block"), ("Dockerfile", "info")]
