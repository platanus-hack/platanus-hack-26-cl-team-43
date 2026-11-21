"""Familia `writes`: escrituras sobre recursos ajenos, medidas leyendo como el dueño antes y después (#24)."""

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from cachai import redteam  # noqa: E402
from tests.test_authn import Served  # noqa: E402

USERS = {"tok-ana": "ana", "tok-beto": "beto"}
INV = redteam.Inventory.from_dict({
    "users": {"ana": {"Authorization": "Bearer tok-ana"}, "beto": {"Authorization": "Bearer tok-beto"}},
    "records": [{"id": "1", "owner": "ana", "markers": ["CNR-ANA-7f3a"]}, {"id": "2", "owner": "beto", "markers": ["CNR-BETO-1d8e"]}],
})


class NoteIn(BaseModel):
    body: str
    pinned: bool = False


def notes_app(check_owner: bool) -> FastAPI:
    notes = {1: {"owner": "ana", "body": "CNR-ANA-7f3a"}, 2: {"owner": "beto", "body": "CNR-BETO-1d8e"}}
    app = FastAPI()

    def get(note_id: int, authorization: str | None, write: bool = False) -> dict:
        user = USERS.get((authorization or "").removeprefix("Bearer ").strip())
        if user is None:
            raise HTTPException(401)
        note = notes.get(note_id)
        if note is None or ((check_owner or not write) and note["owner"] != user):
            raise HTTPException(404)
        return note

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/notes/{note_id}")
    def read(note_id: int, authorization: str | None = Header(default=None)):
        return {"body": get(note_id, authorization)["body"]}

    @app.put("/notes/{note_id}")
    def replace(note_id: int, note: NoteIn, authorization: str | None = Header(default=None)):
        get(note_id, authorization, write=True)["body"] = note.body
        return {"ok": True}

    @app.delete("/notes/{note_id}")
    def delete(note_id: int, authorization: str | None = Header(default=None)):
        get(note_id, authorization, write=True)
        notes.pop(note_id)
        return {"ok": True}

    return app


def _writes(app):
    with Served(app) as srv:
        return redteam.generate(srv.url, INV, families=("writes",))


def test_put_and_delete_without_owner_check_are_detected():
    found = _writes(notes_app(check_owner=False))
    methods = {a.request["method"] for a in found}
    assert "PUT" in methods and {a.family for a in found} == {"writes"}
    put = next(a for a in found if a.request["method"] == "PUT")
    assert put.request["json"]["body"].startswith("CNR-WRITE-") and put.request["json"]["pinned"] is True


def test_owner_checked_writes_give_no_false_alarm():
    assert _writes(notes_app(check_owner=True)) == []


def test_example_body_follows_the_schema():
    comps = {"schemas": {"N": {"type": "object", "properties": {"t": {"type": "string"}, "n": {"type": "integer"},
                                                                  "tags": {"type": "array", "items": {"type": "string"}},
                                                                  "kind": {"enum": ["a", "b"]}}}}}
    assert redteam.example_body({"$ref": "#/components/schemas/N"}, comps, "M") == {"t": "M", "n": 1, "tags": ["M"], "kind": "a"}
