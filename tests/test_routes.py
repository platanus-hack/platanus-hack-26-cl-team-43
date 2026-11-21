"""Rutas leídas del código, sin OpenAPI (#23)."""

import importlib.util
import sys
from pathlib import Path

import pytest

from cachai import redteam, routes

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "rutas"
INV = redteam.Inventory.from_dict({
    "users": {"ana": {"Authorization": "Bearer tok-ana"}, "beto": {"Authorization": "Bearer tok-beto"}},
    "records": [{"id": "1", "owner": "ana", "markers": ["CNR-ANA-7f3a"]}, {"id": "2", "owner": "beto", "markers": ["CNR-BETO-1d8e"]}],
})


def found(path):
    return {(r.method, r.template): r for r in routes.extract(path)}


def test_flask_routes_with_blueprint_prefix_and_query_params():
    r = found(EXAMPLES / "flask_app")
    assert set(r) == {("GET", "/health"), ("GET", "/api/notas/{nota_id}"), ("PUT", "/api/notas/{nota_id}"),
                      ("DELETE", "/api/notas/{nota_id}"), ("GET", "/api/notas/buscar"),
                      ("GET", "/usuarios/{usuario}/archivos/{nombre}")}
    assert r["GET", "/api/notas/buscar"].query_params == ["q", "pagina"]
    assert r["GET", "/usuarios/{usuario}/archivos/{nombre}"].path_params == ["usuario", "nombre"]
    assert r["GET", "/api/notas/{nota_id}"].source.startswith("app.py:")


def test_express_routes_and_query_params():
    r = found(EXAMPLES / "express_app")
    assert set(r) == {("GET", "/health"), ("GET", "/facturas/{facturaId}"), ("PUT", "/facturas/{facturaId}"),
                      ("GET", "/adjuntos"), ("DELETE", "/facturas/{facturaId}/lineas/{linea}")}
    assert set(r["GET", "/adjuntos"].query_params) == {"nombre", "motivo"}


@pytest.mark.parametrize("app", ["flask_app", "express_app"])
def test_red_team_builds_candidates_for_every_route_with_an_id(app):
    static = [x.to_dict() for x in routes.extract(EXAMPLES / app)]
    merged, hidden = routes.merge([], static)
    cands = redteam.candidates(INV, merged, ("authz",))
    with_id = {x["template"] for x in static if x["method"] == "GET" and x["path_params"]}
    assert with_id
    attacked = {c.owner_request["path"] for c in cands}
    for t in with_id:
        assert any(p.startswith(t.split("{", 1)[0]) for p in attacked), t


def test_django_urls():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "urls.py").write_text('from django.urls import path\nurlpatterns = [path("notas/<int:pk>/", v), path("", h)]\n')
        r = found(Path(d))
    assert ("GET", "/notas/{pk}/") in r and r["GET", "/notas/{pk}/"].path_params == ["pk"]


HIDDEN_APP = '''
from fastapi import FastAPI, Header, HTTPException

NOTES = {1: ("ana", "CNR-ANA-7f3a"), 2: ("beto", "CNR-BETO-1d8e")}
USERS = {"tok-ana": "ana", "tok-beto": "beto"}
app = FastAPI()


def user(authorization):
    u = USERS.get((authorization or "").removeprefix("Bearer ").strip())
    if u is None:
        raise HTTPException(401)
    return u


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/notes/{note_id}")
def read(note_id: int, authorization: str | None = Header(default=None)):
    owner, body = NOTES[note_id]
    if owner != user(authorization):
        raise HTTPException(404)
    return {"body": body}


@app.get("/notes/{note_id}/raw", include_in_schema=False)
def raw(note_id: int, authorization: str | None = Header(default=None)):
    user(authorization)
    return {"body": NOTES[note_id][1]}
'''


def test_route_hidden_from_openapi_is_attacked_and_detected(tmp_path):
    pytest.importorskip("fastapi")
    from tests.test_authn import Served
    (tmp_path / "oculta.py").write_text(HIDDEN_APP)
    spec = importlib.util.spec_from_file_location("oculta_app", tmp_path / "oculta.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["oculta_app"] = mod
    spec.loader.exec_module(mod)
    static = [x.to_dict() for x in routes.extract(tmp_path)]
    with Served(mod.app) as srv:
        assert redteam.generate(srv.url, INV, families=("authz",)) == []  # solo con el OpenAPI no se ve
        info = {}
        found_ = redteam.generate(srv.url, INV, families=("authz",), static_routes=static, info=info)
    assert found_ and {a.request["path"] for a in found_} == {"/notes/1/raw", "/notes/2/raw"}
    assert [h["template"] for h in info["hidden_routes"]] == ["/notes/{note_id}/raw"]
