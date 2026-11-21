"""Familia `sqli`: inyección SQL medida con marcas que ningún pedido legítimo devuelve (#26)."""

import sqlite3

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI, Header, HTTPException, Query  # noqa: E402

from cachai import redteam  # noqa: E402
from tests.test_authn import FACTURAS, Served, _load_facturas  # noqa: E402

USERS = {"tok-ana": "ana", "tok-beto": "beto"}
INV = redteam.Inventory.from_dict({
    "users": {"ana": {"Authorization": "Bearer tok-ana"}, "beto": {"Authorization": "Bearer tok-beto"}},
    "records": [{"id": "1", "owner": "ana", "markers": ["CNR-ANA-7f3a9c"]},
                {"id": "2", "owner": "beto", "markers": ["CNR-BETO-1d8e44"]}],
})


def notes_app(concatenated: bool) -> FastAPI:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute("CREATE TABLE notes (id TEXT, owner TEXT, body TEXT)")
    db.executemany("INSERT INTO notes VALUES (?, ?, ?)", [("1", "ana", "CNR-ANA-7f3a9c"), ("2", "beto", "CNR-BETO-1d8e44")])
    app = FastAPI()

    def user(authorization):
        u = USERS.get((authorization or "").removeprefix("Bearer ").strip())
        if u is None:
            raise HTTPException(401)
        return u

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/notes/search")
    def search(q: str = Query(...), authorization: str | None = Header(default=None)):
        me = user(authorization)
        try:
            if concatenated:
                rows = db.execute(f"SELECT id, body FROM notes WHERE id = '{q}' AND owner = '{me}'").fetchall()
            else:
                rows = db.execute("SELECT id, body FROM notes WHERE id = ? AND owner = ?", (q, me)).fetchall()
        except sqlite3.Error:
            raise HTTPException(400)
        return [{"id": i, "body": b} for i, b in rows]

    return app


def _sqli(app, inv=INV):
    with Served(app) as srv:
        return redteam.generate(srv.url, inv, families=("sqli",))


def test_concatenated_query_is_detected():
    found = _sqli(notes_app(concatenated=True))
    assert found and {a.family for a in found} == {"sqli"}
    assert {a.group for a in found} >= {"quote-or-comment"}
    # Cada usuario, con su propio id más la inyección, recibe la nota del otro.
    paths = {(a.request["headers"]["Authorization"], a.request["path"]) for a in found}
    assert ("Bearer tok-ana", "/notes/search?q=1%27%20OR%201%3D1%20--%20") in paths
    assert all(a.control is None for a in found)


def test_parameterized_query_gives_no_false_alarm():
    assert _sqli(notes_app(concatenated=False)) == []


def test_facturas_without_sql_gives_no_false_alarm():
    from cachai.case import load_case
    inv = redteam.Inventory.from_dict(load_case(FACTURAS).inventory)
    assert _sqli(_load_facturas(None), inv) == []


def test_only_the_requested_families_are_generated():
    routes = [("/notes/{note_id}", ["note_id"], []), ("/notes/search", [], ["q"])]
    only = redteam.candidates(INV, routes, ("sqli",))
    assert only and {c.family for c in only} == {"sqli"}
    assert {c.family for c in redteam.candidates(INV, routes)} >= {"authz", "authn", "sqli"}
