"""Familia `ssrf`: el servidor de la app va a buscar una URL que elige quien ataca (#27)."""

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI, Header, HTTPException, Query  # noqa: E402

from cachai import redteam  # noqa: E402
from tests.test_authn import Served  # noqa: E402

INV = redteam.Inventory.from_dict({
    "users": {"ana": {"Authorization": "Bearer tok-ana"}, "beto": {"Authorization": "Bearer tok-beto"}},
    "records": [{"id": "1", "owner": "ana", "markers": ["CNR-ANA-1"]}],
})
ALLOWED = ("https://img.example.com/",)


def preview_app(allowlist: bool) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/preview")
    def preview(url: str = Query(...), authorization: str | None = Header(default=None)):
        if authorization not in ("Bearer tok-ana", "Bearer tok-beto"):
            raise HTTPException(401)
        if allowlist and not url.startswith(ALLOWED):
            raise HTTPException(400, "dominio no permitido")
        try:
            r = httpx.get(url, timeout=2)
            return {"status": r.status_code, "size": len(r.content)}
        except httpx.HTTPError:
            raise HTTPException(502)

    @app.get("/search")
    def search(q: str = Query(...)):
        return {"q": q}  # parámetro sin nombre de URL: no se ataca

    return app


def _ssrf(app):
    canary = redteam.Canary(host="127.0.0.1", port=0)
    try:
        with Served(app) as srv:
            return redteam.generate(srv.url, INV, families=("ssrf",), canary=canary, canary_host="127.0.0.1")
    finally:
        canary.close()


def test_endpoint_that_downloads_a_user_url_is_detected():
    (a,) = _ssrf(preview_app(allowlist=False))
    assert a.family == "ssrf" and a.request["path"].startswith("/preview?url=http%3A%2F%2F127.0.0.1%3A")
    assert a.id in a.request["path"]


def test_allowlist_gives_no_false_alarm():
    assert _ssrf(preview_app(allowlist=True)) == []


def test_without_canary_there_are_no_ssrf_candidates():
    routes = [("/preview", [], ["url"]), ("/search", [], ["q"])]
    assert redteam.candidates(INV, routes, ("ssrf",)) == []
    (c,) = redteam.candidates(INV, routes, ("ssrf",), "http://cachai-canario:8999")
    assert c.request["path"].startswith("/preview?url=http%3A%2F%2Fcachai-canario%3A8999%2Fssrf%2F")
