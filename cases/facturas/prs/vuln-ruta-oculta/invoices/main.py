"""API de facturas de demo.

Rama base del caso: tiene a propósito una falla de autorización por recurso
(cualquier usuario autenticado puede leer facturas ajenas cambiando el id) en
varias rutas y una de acceso a archivos en adjuntos. Solo se usa dentro del
sandbox de Cachai.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

USERS = {"tok-alice": "alice", "tok-bob": "bob"}

INVOICES = {
    1: {"id": 1, "number": "FAC-A-001", "owner": "alice", "amount": 1200, "tax_rate": 0.19},
    2: {"id": 2, "number": "FAC-A-002", "owner": "alice", "amount": 800, "tax_rate": 0.19},
    3: {"id": 3, "number": "FAC-B-001", "owner": "bob", "amount": 450, "tax_rate": 0.19},
}

app = FastAPI(title="Facturas")


def current_user(authorization: str | None) -> str:
    token = (authorization or "").removeprefix("Bearer ").strip()
    user = USERS.get(token)
    if user is None:
        raise HTTPException(status_code=401, detail="no autenticado")
    return user


def get_invoice(invoice_id: int) -> dict:
    inv = INVOICES.get(invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="no existe")
    return inv


def total(inv: dict) -> float:
    return round(inv["amount"] * (1 + inv["tax_rate"]), 2)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/invoices")
def list_invoices(authorization: str | None = Header(default=None)) -> list[dict]:
    user = current_user(authorization)
    return [inv for inv in INVOICES.values() if inv["owner"] == user]


@app.get("/invoices/export")
def export_invoices(ids: str = Query(...), authorization: str | None = Header(default=None)) -> list[dict]:
    current_user(authorization)
    return [get_invoice(int(i)) for i in ids.split(",") if i.strip()]


@app.get("/invoices/{invoice_id}")
def read_invoice(invoice_id: int, authorization: str | None = Header(default=None)) -> dict:
    current_user(authorization)
    return get_invoice(invoice_id)


@app.get("/invoices/{invoice_id}/total")
def invoice_total(invoice_id: int, authorization: str | None = Header(default=None)) -> dict:
    current_user(authorization)
    inv = get_invoice(invoice_id)
    return {"id": inv["id"], "total": total(inv)}


@app.get("/invoices/{invoice_id}/pdf", response_class=PlainTextResponse)
def invoice_pdf(invoice_id: int, authorization: str | None = Header(default=None)) -> str:
    current_user(authorization)
    inv = get_invoice(invoice_id)
    return f"PDF {inv['number']} cliente={inv['owner']} monto={inv['amount']}"


@app.get("/invoices/{invoice_id}/raw", include_in_schema=False)
def invoice_raw(invoice_id: int, authorization: str | None = Header(default=None)) -> dict:
    """Vista interna para soporte; no se publica en la documentación."""
    current_user(authorization)
    return get_invoice(invoice_id)


@app.get("/v2/invoices/{invoice_id}")
def read_invoice_v2(invoice_id: int, authorization: str | None = Header(default=None)) -> dict:
    current_user(authorization)
    inv = get_invoice(invoice_id)
    return {"data": inv, "version": 2}


@app.get("/attachments", response_class=PlainTextResponse)
def read_attachment(name: str = Query(...), authorization: str | None = Header(default=None)) -> str:
    user = current_user(authorization)
    path = DATA_DIR / "attachments" / user / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no existe")
    return path.read_text()
