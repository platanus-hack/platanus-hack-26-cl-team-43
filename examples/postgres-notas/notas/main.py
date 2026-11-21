"""API de notas sobre Postgres. Ejemplo para Cachai: la app y su base de datos levantan con el compose del repo."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Header, HTTPException

USERS = {"tok-ana": "ana", "tok-beto": "beto"}
SEED = [(1, "ana", "Nota privada de Ana: NOTA-ANA-7Q2"), (2, "beto", "Nota privada de Beto: NOTA-BETO-4K9")]


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS notes (id int PRIMARY KEY, owner text NOT NULL, body text NOT NULL)")
        db.cursor().executemany("INSERT INTO notes VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING", SEED)
    yield


app = FastAPI(title="Notas", lifespan=lifespan)


def current_user(authorization: str | None) -> str:
    user = USERS.get((authorization or "").removeprefix("Bearer ").strip())
    if user is None:
        raise HTTPException(status_code=401, detail="no autenticado")
    return user


@app.get("/health")
def health() -> dict:
    with connect() as db:
        db.execute("SELECT 1")
    return {"ok": True}


@app.get("/notes")
def list_notes(authorization: str | None = Header(default=None)) -> list[dict]:
    user = current_user(authorization)
    with connect() as db:
        rows = db.execute("SELECT id, body FROM notes WHERE owner = %s ORDER BY id", (user,)).fetchall()
    return [{"id": i, "body": b} for i, b in rows]


@app.get("/notes/{note_id}")
def read_note(note_id: int, authorization: str | None = Header(default=None)) -> dict:
    user = current_user(authorization)
    with connect() as db:
        row = db.execute("SELECT id, owner, body FROM notes WHERE id = %s", (note_id,)).fetchone()
    if row is None or row[1] != user:
        raise HTTPException(status_code=404, detail="no existe")
    return {"id": row[0], "body": row[2]}
