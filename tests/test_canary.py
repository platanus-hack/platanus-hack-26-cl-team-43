"""Marcas canario por corrida y semilla en la configuración (#21)."""

import json

import pytest

from cachai import canary
from cachai.case import ConfigError, load_target

INV = {"users": {"ana": {"A": "1"}, "beto": {"A": "2"}},
       "records": [{"id": "1", "owner": "ana", "markers": ["{{canary:nota_ana}}"]},
                   {"id": "2", "owner": "beto", "markers": ["fijo-beto", "x {{canary:nota_beto}} y"]}]}


def test_each_run_gets_new_values():
    names = canary.names(INV)
    assert names == {"nota_ana", "nota_beto"}
    a, b = canary.generate(names), canary.generate(names)
    assert a.keys() == b.keys() and all(a[k] != b[k] for k in a)


def test_resolve_replaces_every_placeholder_and_keeps_the_rest():
    values = {"nota_ana": "CNR-1", "nota_beto": "CNR-2"}
    r = canary.resolve(INV, values)
    assert r["records"][0]["markers"] == ["CNR-1"] and r["records"][1]["markers"] == ["fijo-beto", "x CNR-2 y"]
    assert INV["records"][0]["markers"] == ["{{canary:nota_ana}}"]  # no muta el original
    assert canary.env(values) == {"CACHAI_CANARY_NOTA_ANA": "CNR-1", "CACHAI_CANARY_NOTA_BETO": "CNR-2"}
    with pytest.raises(ValueError):
        canary.resolve(INV, {"nota_ana": "x"})


COMPOSE_APP = {"compose": {"file": "docker-compose.yml", "service": "api"}}


def test_seed_loads_and_roundtrips():
    t = load_target(json.dumps({"version": 2, "app": COMPOSE_APP, "seed": {"command": ["python", "-m", "s"]}}))
    assert t.seed == ("python", "-m", "s") and load_target(json.dumps(t.to_dict())) == t


@pytest.mark.parametrize("seed, app, field", [
    ({"command": []}, COMPOSE_APP, "seed.command"),
    ({"command": "python s.py"}, COMPOSE_APP, "seed.command"),
    ({"command": ["x"], "shell": True}, COMPOSE_APP, "seed.shell"),
    ({"command": ["x"]}, {"module": "a:b"}, "seed"),  # sin imagen propia no hay dónde correrla
])
def test_invalid_seed_names_the_field(seed, app, field):
    with pytest.raises(ConfigError) as e:
        load_target(json.dumps({"version": 2, "app": app, "seed": seed}))
    assert e.value.field == field
