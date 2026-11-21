"""Familia `authn`: rutas que entregan datos sin una sesión válida (#15)."""

import importlib.util
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from cachai import redteam
from cachai.case import load_case
from cachai.holdout import Attack, run_attacks

FACTURAS = Path(__file__).resolve().parent.parent / "cases" / "facturas"

INV = redteam.Inventory.from_dict({
    "users": {"ana": {"Authorization": "Bearer a"}, "beto": {"Authorization": "Bearer b"}},
    "records": [{"id": "1", "owner": "ana", "markers": ["DOC-A"]}, {"id": "2", "owner": "beto", "markers": ["DOC-B"]}],
})


def test_anonymous_variants_keep_the_scheme():
    assert redteam.anonymous_headers(INV) == {
        "none": {},
        "empty": {"Authorization": "Bearer"},
        "invalid": {"Authorization": "Bearer " + redteam.INVALID_TOKEN},
    }
    raw = redteam.Inventory.from_dict({"users": {"a": {"X-Api-Key": "k1"}, "b": {"X-Api-Key": "k2"}}})
    assert redteam.anonymous_headers(raw)["invalid"] == {"X-Api-Key": redteam.INVALID_TOKEN}
    basic = redteam.Inventory.from_dict({"users": {"a": {"Authorization": "Basic YTpi"}, "b": {"Authorization": "basic Yjpj"}}})
    assert redteam.anonymous_headers(basic)["invalid"]["Authorization"].lower() == "basic " + redteam.INVALID_TOKEN


def test_anonymous_variants_never_carry_a_real_session():
    """Una cookie con varios campos no tiene esquema: nada del valor real se conserva."""
    inv = redteam.Inventory.from_dict({"users": {
        "a": {"Cookie": "session=tok-a; lang=es"}, "b": {"Cookie": "session=tok-b; lang=es"}}})
    anon = redteam.anonymous_headers(inv)
    assert anon == {"none": {}, "empty": {"Cookie": ""}, "invalid": {"Cookie": redteam.INVALID_TOKEN}}
    real = {v for u in inv.users.values() for v in u.values()}
    assert not any(part in v for h in anon.values() for v in h.values() for r in real for part in r.split("; "))


def test_shared_headers_are_kept_so_the_app_reaches_the_session_check():
    inv = redteam.Inventory.from_dict({"users": {
        "a": {"Authorization": "Bearer a", "X-Tenant": "acme"}, "b": {"Authorization": "Bearer b", "X-Tenant": "acme"}}})
    anon = redteam.anonymous_headers(inv)
    assert anon["none"] == {"X-Tenant": "acme"}
    assert anon["invalid"] == {"X-Tenant": "acme", "Authorization": "Bearer " + redteam.INVALID_TOKEN}


def test_candidates_cover_every_route_without_a_session():
    routes = [("/docs/{id}", ["id"], []), ("/buscar", [], ["id"]), ("/docs", [], [])]
    cands = [c for c in redteam.candidates(INV, routes) if c.family == "authn"]
    by_group: dict[str, list] = {}
    for c in cands:
        by_group.setdefault(c.group, []).append(c)
    assert set(by_group) == {redteam.make_id("/docs/{id}"), redteam.make_id("/buscar", "id"), redteam.make_id("/docs")}
    # Tres variantes por recurso (o por usuario en el listado), siempre contra el pedido del dueño.
    assert all(len(v) == 6 for v in by_group.values())
    for c in cands:
        assert c.request["path"] == c.owner_request["path"]
        assert c.request["headers"] in redteam.anonymous_headers(INV).values()
        assert c.owner_request["headers"] in INV.users.values()
    listing = by_group[redteam.make_id("/docs")]
    assert {c.markers for c in listing} == {("DOC-A",), ("DOC-B",)}
    assert len({c.id for c in cands}) == len(cands)


def test_query_routes_also_try_the_owners_files():
    inv = redteam.Inventory.from_dict({
        "users": {"ana": {"Authorization": "Bearer a"}, "beto": {"Authorization": "Bearer b"}},
        "files": [{"path": "ana/notas/f.txt", "owner": "ana", "markers": ["ARCHIVO-A"]}],
    })
    cands = [c for c in redteam.candidates(inv, [("/adjuntos", [], ["name"])]) if c.family == "authn"]
    assert {c.request["path"] for c in cands} == {"/adjuntos?name=notas/f.txt"}
    assert len(cands) == 3 and all(c.owner_request["headers"] == {"Authorization": "Bearer a"} for c in cands)


class HeaderOnlyApp:
    """Comprueba que venga un Authorization, pero no que el token exista."""

    def __init__(self):
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/openapi.json":
                    status, body = 200, json.dumps({"paths": {"/docs/{id}": {"get": {"parameters": [{"name": "id", "in": "path"}]}}}})
                elif not self.headers.get("Authorization"):
                    status, body = 401, "no"
                elif self.path == "/docs/1" and self.headers["Authorization"] in ("Bearer a", "Bearer " + redteam.INVALID_TOKEN, "Bearer"):
                    status, body = 200, "DOC-A"
                elif self.path == "/docs/2" and self.headers["Authorization"] != "Bearer a":
                    status, body = 200, "DOC-B"
                else:
                    status, body = 404, "no"
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def test_each_variant_is_confirmed_on_its_own():
    """Sin header queda bloqueado; con header vacío o token inválido, la fuga se confirma."""
    app = HeaderOnlyApp()
    try:
        found = [a for a in redteam.generate(app.url, INV) if a.family == "authn"]
    finally:
        app.srv.shutdown()
    assert {(a.request["path"], a.request["headers"].get("Authorization")) for a in found} == {
        ("/docs/1", "Bearer"), ("/docs/1", "Bearer " + redteam.INVALID_TOKEN),
        ("/docs/2", "Bearer"), ("/docs/2", "Bearer " + redteam.INVALID_TOKEN)}


def test_authn_attacks_roundtrip_and_validate():
    c = next(c for c in redteam.candidates(INV, [("/docs/{id}", ["id"], [])]) if c.family == "authn")
    from cachai.holdout import Control, Predicate
    leak = Predicate(body_contains_any=c.markers)
    a = Attack(c.id, "authn", c.request, leak, c.group, Control(c.owner_request, leak))
    assert Attack.from_dict(a.to_dict()) == a


# --- facturas, en proceso ----------------------------------------------------

def _load_facturas(extra_route: str | None):
    """La app de facturas de la base; con `extra_route`, un endpoint nuevo
    `sin-sesion` (lo que deja un agente apurado: olvida current_user) o `con-sesion`."""
    spec = importlib.util.spec_from_file_location(f"facturas_main_{extra_route}", FACTURAS / "base" / "invoices" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if extra_route:
        from fastapi import Header

        @mod.app.get("/invoices/{invoice_id}/resumen")
        def resumen(invoice_id: int, authorization: str | None = Header(default=None)) -> dict:
            if extra_route == "con-sesion":
                user = mod.current_user(authorization)
                inv = mod.get_invoice(invoice_id)
                if inv["owner"] != user:
                    raise mod.HTTPException(status_code=404, detail="no existe")
            inv = mod.get_invoice(invoice_id)
            return {"number": inv["number"], "total": mod.total(inv)}
    return mod.app


class Served:
    def __init__(self, app):
        import uvicorn
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("la app no arrancó")
            time.sleep(0.02)
        self.url = f"http://127.0.0.1:{port}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)


def _authn(app) -> list[Attack]:
    inv = redteam.Inventory.from_dict(load_case(FACTURAS).inventory)
    with Served(app) as srv:
        return [a for a in redteam.generate(srv.url, inv) if a.family == "authn"]


def test_facturas_current_routes_give_no_false_alarms():
    assert _authn(_load_facturas(None)) == []


def test_facturas_new_route_without_current_user_is_detected():
    found = _authn(_load_facturas("sin-sesion"))
    assert found
    assert {a.group for a in found} == {redteam.make_id("/invoices/{invoice_id}/resumen")}
    assert {a.request["path"] for a in found} == {"/invoices/1/resumen", "/invoices/2/resumen", "/invoices/3/resumen"}
    # Las tres formas de pedir sin sesión, y el dueño como control.
    assert {tuple(a.request["headers"].items()) for a in found} == {
        (), (("Authorization", "Bearer"),), (("Authorization", "Bearer " + redteam.INVALID_TOKEN),)}
    for a in found:
        assert a.control.request["path"] == a.request["path"]
        assert a.control.request["headers"]["Authorization"] in ("Bearer tok-alice", "Bearer tok-bob")


def test_the_same_attacks_are_blocked_once_the_route_asks_for_a_session():
    """De punta a punta: los ataques confirmados contra la ruta rota quedan
    bloqueados cuando el PR agrega current_user, y el control del dueño pasa."""
    found = _authn(_load_facturas("sin-sesion"))
    with Served(_load_facturas("con-sesion")) as srv:
        rep = run_attacks(srv.url, found)
    assert rep.complete and rep.confirmed == []
    assert {r.outcome for r in rep.results} == {"blocked"}
