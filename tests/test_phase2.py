"""Fase 2: ejecución de ataques, reparto por grupos, equipo rojo y evidencia de ataques."""

import http.server
import json
import threading
import time
from pathlib import Path

import pytest

from cachai import pipeline, redteam
from cachai.case import load_case
from cachai.gate import decide
from cachai.holdout import Attack, Control, Predicate, run_attacks, searchable, split, split_record

ROOT = Path(__file__).resolve().parent.parent
FACTURAS = ROOT / "cases" / "facturas"


class Server:
    """Servidor local con respuestas fijas por ruta: {ruta: (status, cuerpo, espera)}."""

    def __init__(self, routes, headers_seen=None):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.paths.append(self.path)
                if headers_seen is not None:
                    headers_seen.append(dict(self.headers))
                status, body, delay = routes.get(self.path, (404, "no", 0))
                time.sleep(delay)
                self.send_response(status)
                if status in (301, 302, 307):
                    self.send_header("Location", body)
                    body = ""
                self.send_header("Content-Length", str(len(body.encode())))
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        self.paths: list[str] = []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def attack(id, path, contains="SECRETO", group="", family="authz", control=None):
    return Attack(id, family, {"method": "GET", "path": path, "headers": {"Authorization": "Bearer x"}},
                  Predicate(body_contains_any=(contains,)), group, control)


def control(path, contains="SECRETO"):
    return Control({"method": "GET", "path": path, "headers": {"Authorization": "Bearer dueno"}}, Predicate(body_contains_any=(contains,)))


# --- run_attacks ------------------------------------------------------------

def test_run_attacks_confirmed_blocked_and_error():
    srv = Server({"/a": (200, "SECRETO", 0), "/b": (403, "prohibido", 0), "/c": (200, "nada", 0)})
    try:
        rep = run_attacks(srv.url, [attack("a", "/a"), attack("b", "/b"), attack("c", "/c")])
    finally:
        srv.close()
    assert [(r.id, r.outcome, r.http_status) for r in rep.results] == [("a", "confirmed", 200), ("b", "blocked", 403), ("c", "blocked", 200)]
    assert rep.complete and rep.confirmed == ["a"]


@pytest.mark.parametrize("status", [201, 206, 403, 404, 500])
def test_leaked_data_counts_with_any_status(status):
    """Los datos de otro usuario en la respuesta son una fuga con cualquier código."""
    srv = Server({"/a": (status, "aquí va SECRETO", 0)})
    try:
        rep = run_attacks(srv.url, [attack("a", "/a")])
    finally:
        srv.close()
    assert (rep.results[0].outcome, rep.results[0].http_status) == ("confirmed", status)


def test_same_origin_redirect_is_followed_and_leak_is_found():
    srv = Server({"/r": (302, "/paso", 0), "/paso": (307, "/fuga", 0), "/fuga": (200, "SECRETO", 0)})
    try:
        rep = run_attacks(srv.url, [attack("r", "/r")])
    finally:
        srv.close()
    assert rep.results[0].outcome == "confirmed" and srv.paths == ["/r", "/paso", "/fuga"]


def test_redirect_to_another_origin_is_not_followed():
    other = Server({"/fuga": (200, "SECRETO", 0)})
    srv = Server({"/r": (302, other.url + "/fuga", 0)})
    try:
        rep = run_attacks(srv.url, [attack("r", "/r")])
    finally:
        srv.close()
        other.close()
    # No se sigue, y tampoco se da por bloqueado: quedó un destino sin inspeccionar.
    assert rep.results[0].outcome == "error" and "otro origen" in rep.results[0].error and other.paths == []
    assert not rep.complete


def chain(n, final=(200, "SECRETO", 0)):
    """Ruta /0 que redirige n veces antes de llegar al recurso."""
    routes = {f"/{i}": (302, f"/{i + 1}", 0) for i in range(n)}
    routes[f"/{n}"] = final
    return routes


def test_redirect_chain_at_the_limit_is_inspected():
    srv = Server(chain(5))
    try:
        rep = run_attacks(srv.url, [attack("r", "/0")])
    finally:
        srv.close()
    assert rep.results[0].outcome == "confirmed" and len(srv.paths) == 6


def test_redirect_chain_past_the_limit_has_no_verdict():
    """El recurso está un salto más allá de lo que se inspecciona: no es un bloqueo."""
    srv = Server(chain(6))
    try:
        rep = run_attacks(srv.url, [attack("r", "/0", control=control("/6"))])
    finally:
        srv.close()
    assert rep.results[0].outcome == "error" and "redirecciones" in rep.results[0].error and not rep.complete


def test_redirect_loop_has_no_verdict():
    srv = Server({"/r": (302, "/r", 0)})
    try:
        rep = run_attacks(srv.url, [attack("r", "/r")])
    finally:
        srv.close()
    assert rep.results[0].outcome == "error" and len(srv.paths) == 6


def test_leak_seen_before_an_unfinished_chain_still_counts():
    class Leaky(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"SECRETO"
            self.send_response(302)
            self.send_header("Location", "/otra")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Leaky)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rep = run_attacks(f"http://127.0.0.1:{srv.server_address[1]}", [attack("r", "/r")])
    finally:
        srv.shutdown()
    assert rep.results[0].outcome == "confirmed"


def test_unfinished_chain_in_the_control_has_no_verdict():
    srv = Server({"/a": (403, "no", 0), "/c": (302, "/c", 0)})
    try:
        rep = run_attacks(srv.url, [attack("a", "/a", control=control("/c"))])
    finally:
        srv.close()
    assert rep.results[0].outcome == "error" and rep.results[0].error.startswith("control:")


class SessionApp:
    """App que autoriza por sesión: el pedido del dueño deja una cookie y con ella se lee el recurso."""

    def __init__(self):
        outer = self
        self.cookies = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                who = self.headers.get("Authorization", "")
                cookie = self.headers.get("Cookie", "")
                outer.cookies.append((who, cookie))
                allowed = who == "Bearer dueno" or "session=dueno" in cookie
                body = b"SECRETO" if allowed else b"no"
                self.send_response(200 if allowed else 403)
                if who == "Bearer dueno":
                    self.send_header("Set-Cookie", "session=dueno; Path=/")
                else:
                    self.send_header("Set-Cookie", "visto=1; Path=/")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def test_owner_session_cookie_never_reaches_the_attacker():
    app = SessionApp()
    try:
        rep = run_attacks(app.url, [attack("a", "/a", control=control("/a")), attack("b", "/b", control=control("/b"))])
    finally:
        app.srv.shutdown()
    assert [r.outcome for r in rep.results] == ["blocked", "blocked"]
    attacker = [cookie for who, cookie in app.cookies if who == "Bearer x"]
    assert all("session=dueno" not in c for c in attacker)
    # Cada identidad sí conserva su propia sesión entre pedidos.
    assert attacker == ["", "visto=1"]
    assert [cookie for who, cookie in app.cookies if who == "Bearer dueno"] == ["", "session=dueno"]


class ScopedSessionApp:
    """La sesión vive en una cookie limitada a /docs: sin ella nadie lee, con ella lee cualquiera."""

    def __init__(self):
        outer = self
        self.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                who = self.headers.get("Authorization", "")
                cookie = self.headers.get("Cookie", "")
                outer.seen.append((who, self.path, cookie))
                if self.path == "/login":
                    body, status = b"ok", 200
                else:
                    body, status = (b"SECRETO", 200) if "sid=" in cookie else (b"no", 401)
                self.send_response(status)
                if self.path == "/login":
                    self.send_header("Set-Cookie", f"sid={who[-1]}; Path=/docs")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def test_session_cookie_scoped_to_a_subpath_is_sent():
    """Un usuario con sesión en /docs la sigue usando: si la app deja leer lo ajeno, se ve."""
    from cachai.holdout import Clients, exchange
    app = ScopedSessionApp()
    owner = {"method": "GET", "path": "/docs/1", "headers": {"Authorization": "Bearer dueno"}}
    other = {"method": "GET", "path": "/docs/1", "headers": {"Authorization": "Bearer x"}}
    try:
        with Clients() as clients:
            for req in (owner, other):
                exchange(clients.for_request(req), app.url, {**req, "path": "/login"}, time.monotonic() + 10)
            hops, pending = exchange(clients.for_request(other), app.url, other, time.monotonic() + 10)
            owner_hops, _ = exchange(clients.for_request(owner), app.url, owner, time.monotonic() + 10)
    finally:
        app.srv.shutdown()
    assert hops == [(200, "SECRETO")] and pending is None and owner_hops == [(200, "SECRETO")]
    sent = {(who, path): cookie for who, path, cookie in app.seen}
    assert sent[("Bearer x", "/docs/1")] == "sid=x" and sent[("Bearer dueno", "/docs/1")] == "sid=o"


def test_cookie_scoped_to_another_path_is_not_sent():
    from cachai.holdout import Clients, exchange
    app = ScopedSessionApp()
    req = {"method": "GET", "path": "/otra/1", "headers": {"Authorization": "Bearer x"}}
    try:
        with Clients() as clients:
            exchange(clients.for_request(req), app.url, {**req, "path": "/login"}, time.monotonic() + 10)
            hops, _ = exchange(clients.for_request(req), app.url, req, time.monotonic() + 10)
    finally:
        app.srv.shutdown()
    assert hops == [(401, "no")]


def test_raw_path_survives_with_scoped_cookies():
    """Armar la URL antes del pedido no altera la ruta que viaja."""
    from cachai.holdout import Clients, exchange
    seen = []
    srv = Server({}, seen)
    req = {"method": "GET", "path": "/docs/f?name=..%2Fa%2Fx.txt&n=./../y", "headers": {"Authorization": "Bearer x"}}
    try:
        with Clients() as clients:
            exchange(clients.for_request(req), srv.url, req, time.monotonic() + 10)
    finally:
        srv.close()
    assert srv.paths == ["/docs/f?name=..%2Fa%2Fx.txt&n=./../y"]


class DefaultPathSessionApp:
    """Cada lectura de /docs/N deja una cookie sin Path (su ámbito por defecto es /docs).
    Quien ya tiene esa sesión propia recibe el recurso, sea o no el dueño."""

    def __init__(self):
        outer = self
        self.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                who = self.headers.get("Authorization", "")
                cookie = self.headers.get("Cookie", "")
                outer.seen.append((who, self.path, cookie))
                allowed = who == "Bearer dueno" or "sid=" in cookie
                body = f"SECRETO-{self.path[-1]}".encode() if allowed else b"no"
                self.send_response(200)
                self.send_header("Set-Cookie", "sid=1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def test_leak_that_needs_the_callers_own_session_is_confirmed():
    """Igual que un cliente httpx normal: el segundo pedido lleva la cookie y la fuga se ve."""
    import httpx
    app = DefaultPathSessionApp()
    attacks = [attack(f"a{n}", f"/docs/{n}", contains=f"SECRETO-{n}", control=control(f"/docs/{n}", f"SECRETO-{n}")) for n in (1, 2)]
    try:
        rep = run_attacks(app.url, attacks)
        with httpx.Client(base_url=app.url, timeout=10) as c:
            normal = [c.get(f"/docs/{n}", headers={"Authorization": "Bearer y"}).text for n in (1, 2)]
    finally:
        app.srv.shutdown()
    assert normal == ["no", "SECRETO-2"]
    assert [r.outcome for r in rep.results] == ["blocked", "confirmed"]
    assert [c for who, path, c in app.seen if who == "Bearer x"] == ["", "sid=1"]


def test_generator_confirms_a_leak_that_needs_the_callers_own_session():
    app = DefaultPathSessionApp()
    inv = redteam.Inventory.from_dict({
        "users": {"dueno": {"Authorization": "Bearer dueno"}, "otro": {"Authorization": "Bearer x"}},
        "records": [{"id": "1", "owner": "dueno", "markers": ["SECRETO-1"]}, {"id": "2", "owner": "dueno", "markers": ["SECRETO-2"]}],
    })
    try:
        found = redteam.confirm(app.url, redteam.candidates(inv, [("/docs/{id}", ["id"], [])]))
    finally:
        app.srv.shutdown()
    assert [a.request["path"] for a in found if a.family == "authz"] == ["/docs/2"]


def test_generator_uses_separate_sessions_too():
    app = SessionApp()
    inv = redteam.Inventory.from_dict({
        "users": {"dueno": {"Authorization": "Bearer dueno"}, "otro": {"Authorization": "Bearer x"}},
        "records": [{"id": "1", "owner": "dueno", "markers": ["SECRETO"]}],
    })
    try:
        found = redteam.confirm(app.url, redteam.candidates(inv, [("/docs/{id}", ["id"], [])]))
    finally:
        app.srv.shutdown()
    assert found == []  # la app protege bien: con sesiones separadas no hay fuga que confirmar


class Raw:
    """Servidor que responde bytes y Content-Type fijos."""

    def __init__(self, body: bytes, content_type: str | None):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                if content_type:
                    self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.mark.parametrize("body,content_type", [
    ("SECRETO".encode("utf-16"), "text/plain; charset=utf-16"),
    ("SECRETO".encode("utf-16-le"), "text/plain; charset=utf-16-le"),
    ('{"n": "SECRETO"}'.encode("utf-16"), "application/json; charset=utf-16"),
    ("SECRETO".encode("utf-16"), "text/plain"),            # sin charset: lo delata la marca de orden de bytes
    ("\ufeffSECRETO".encode("utf-8"), None),
    ("SECRETÓ y SECRETO".encode("latin-1"), "text/plain; charset=iso-8859-1"),
])
def test_declared_charset_is_honoured(body, content_type):
    srv = Raw(body, content_type)
    try:
        rep = run_attacks(srv.url, [attack("a", "/a")])
    finally:
        srv.srv.shutdown()
    assert rep.results[0].outcome == "confirmed"


@pytest.mark.parametrize("body,content_type", [
    (b"\xff\xfe\xfa SECRETO", "text/plain; charset=utf-8"),   # bytes que no son del charset declarado
    (b"SECRETO", "text/plain; charset=klingon"),               # charset desconocido
    ("SECRETO".encode("utf-16-le"), "text/plain"),             # otra codificación sin declarar: trae bytes nulos
    (b"%PDF-1.7\x00\x01 binario", "application/pdf"),
])
def test_unreadable_response_is_never_blocked(body, content_type):
    srv = Raw(body, content_type)
    try:
        rep = run_attacks(srv.url, [attack("a", "/a")])
    finally:
        srv.srv.shutdown()
    assert rep.results[0].outcome == "error" and not rep.complete


def test_failed_control_is_error_not_blocked():
    """Si el dueño tampoco obtiene su recurso, la app le niega el servicio a este cliente."""
    srv = Server({"/a": (403, "no", 0)})
    try:
        rep = run_attacks(srv.url, [attack("a", "/a", control=control("/a"))])
    finally:
        srv.close()
    assert rep.results[0].outcome == "error" and "control" in rep.results[0].error and not rep.complete


def test_control_ok_and_attack_blocked():
    class Auth(Server):
        pass
    seen = []
    srv = Server({"/a": (200, "SECRETO", 0)}, seen)
    try:
        rep = run_attacks(srv.url, [attack("a", "/a", control=control("/a"))])
    finally:
        srv.close()
    assert rep.results[0].outcome == "confirmed" and srv.paths == ["/a", "/a"]
    assert [h["Authorization"] for h in seen] == ["Bearer dueno", "Bearer x"]


def test_attack_client_looks_like_the_test_client(monkeypatch):
    """Mismos headers que el cliente httpx de los tests en el evaluador (con el perfil de
    navegador de la corrida, #18): la app no puede distinguirlos."""
    import httpx

    from cachai import holdout, pytest_http
    profile = holdout.browser_headers()
    monkeypatch.setattr(httpx.Client, "__init__", httpx.Client.__init__)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", httpx.AsyncClient.__init__)
    pytest_http.use_default_headers(profile)
    seen = []
    srv = Server({}, seen)
    try:
        run_attacks(srv.url, [attack("a", "/a")], headers=profile)
        with httpx.Client(base_url=srv.url, timeout=10) as c:
            c.get("/a", headers={"Authorization": "Bearer x"})
    finally:
        srv.close()
    assert seen[0] == seen[1]
    assert seen[0]["User-Agent"] == profile["User-Agent"]


def test_json_escapes_do_not_hide_the_marker():
    assert "FAC-A-001" in searchable('{"n": "\\u0046AC-A-001"}')
    srv = Server({"/a": (200, '{"n": "\\u0053ECRETO"}', 0)})
    try:
        rep = run_attacks(srv.url, [attack("a", "/a")])
    finally:
        srv.close()
    assert rep.results[0].outcome == "confirmed"


def test_oversized_response_is_error(monkeypatch):
    from cachai import holdout
    monkeypatch.setattr(holdout, "MAX_BODY", 100)
    srv = Server({"/a": (200, "x" * 500 + "SECRETO", 0)})
    try:
        rep = run_attacks(srv.url, [attack("a", "/a")])
    finally:
        srv.close()
    assert rep.results[0].outcome == "error" and "grande" in rep.results[0].error


def test_run_attacks_unreachable_app_is_error_not_blocked():
    rep = run_attacks("http://127.0.0.1:9", [attack("a", "/a")])
    assert rep.results[0].outcome == "error" and not rep.complete and rep.confirmed == []


def test_run_attacks_sends_path_and_headers_verbatim():
    seen = []
    srv = Server({}, seen)
    try:
        run_attacks(srv.url, [attack("a", "/f?name=..%2Falice%2Fx.txt"), attack("b", "/f?name=./../alice/x.txt"), attack("c", "/f?name=%2e%2e/x")])
    finally:
        srv.close()
    assert srv.paths == ["/f?name=..%2Falice%2Fx.txt", "/f?name=./../alice/x.txt", "/f?name=%2e%2e/x"]
    assert seen[0]["Authorization"] == "Bearer x"


def test_run_attacks_past_deadline_is_error():
    srv = Server({"/a": (200, "SECRETO", 0)})
    try:
        rep = run_attacks(srv.url, [attack("a", "/a")], deadline=time.monotonic() - 1)
    finally:
        srv.close()
    assert rep.results[0].outcome == "error" and srv.paths == []


def test_run_attacks_slow_response_is_error():
    srv = Server({"/a": (200, "SECRETO", 1.5)})
    try:
        rep = run_attacks(srv.url, [attack("a", "/a")], deadline=time.monotonic() + 0.3)
    finally:
        srv.close()
    assert rep.results[0].outcome == "error"


# --- reparto por grupos -------------------------------------------------------

def grouped():
    out = []
    for g in ("r1", "r2", "r3", "r4", "r5"):
        out += [attack(f"authz-{g}-{i}", f"/{g}/{i}", group=g) for i in range(3)]
    for g in ("t1", "t2", "t3"):
        out += [attack(f"files-{g}-{i}", f"/f?n={g}{i}", group=g, family="files") for i in range(2)]
    return out


def test_split_keeps_groups_whole_and_covers_each_family():
    vis, res = split(grouped(), shown=0.4, seed=3)
    vg, rg = {a.group_key for a in vis}, {a.group_key for a in res}
    assert not vg & rg
    for fam in ("authz", "files"):
        assert any(f == fam for f, _ in vg) and any(f == fam for f, _ in rg)


def test_split_single_group_family_is_reserved():
    attacks = grouped() + [attack("solo-1", "/s", group="solo", family="files")]
    attacks = [a for a in attacks if a.family == "authz" or a.group == "solo"]
    vis, res = split(attacks, seed=1)
    assert all(a.family == "authz" for a in vis) and any(a.group == "solo" for a in res)


def test_split_needs_two_groups_somewhere():
    with pytest.raises(ValueError):
        split([attack(f"a{i}", f"/{i}", group="uno") for i in range(4)])


def test_group_roundtrip_and_validation():
    a = attack("a1", "/x", group="ruta-1")
    assert Attack.from_dict(a.to_dict()) == a
    with pytest.raises(ValueError):
        attack("a1", "/x", group="Mal Grupo")


# --- equipo rojo --------------------------------------------------------------

INV = redteam.Inventory.from_dict({
    "users": {"ana": {"Authorization": "Bearer a"}, "beto": {"Authorization": "Bearer b"}},
    "records": [{"id": "1", "owner": "ana", "markers": ["DOC-A", "TOTAL-10"]}],
    "files": [{"path": "ana/f.txt", "owner": "ana", "markers": ["ARCHIVO-A"]}],
    "files_root": "/data",
})
OPENAPI = {"paths": {
    "/docs/{doc_id}": {"get": {"parameters": [{"name": "doc_id", "in": "path"}]}},
    "/docs/{doc_id}/total": {"get": {"parameters": [{"name": "doc_id", "in": "path"}]}},
    "/public/{doc_id}": {"get": {"parameters": [{"name": "doc_id", "in": "path"}]}},
    "/docs/{doc_id}/exists": {"get": {"parameters": [{"name": "doc_id", "in": "path"}]}},
    "/aaa": {"get": {"parameters": [{"name": "name", "in": "query"}]}},
    "/safe/{doc_id}": {"get": {"parameters": [{"name": "doc_id", "in": "path"}]}},
    "/files": {"get": {"parameters": [{"name": "name", "in": "query"}]}},
    "/health": {"get": {}},
    "/docs": {"post": {}},
}}


def test_candidates_cover_routes_and_file_templates():
    cands = [c for c in redteam.candidates(INV, redteam.get_routes(OPENAPI)) if c.family != "authn"]
    groups = {(c.family, c.group) for c in cands}
    assert ("authz", redteam.make_id("/docs/{doc_id}")) in groups and ("authz", redteam.make_id("/files", "name")) in groups
    assert {g for f, g in groups if f == "files"} == {t[0] for t in redteam.FILE_TEMPLATES}
    assert all(c.request["headers"] == {"Authorization": "Bearer b"} for c in cands)
    assert all(c.owner_request["headers"] == {"Authorization": "Bearer a"} for c in cands)
    paths = {c.request["path"] for c in cands}
    assert "/files?name=../ana/f.txt" in paths and "/files?name=..%2Fana%2Ff.txt" in paths and "/files?name=/data/ana/f.txt" in paths
    assert not any(p.startswith("/health") for p in paths)
    # Dos rutas con el mismo parámetro no se pisan: cada una tiene sus candidatos.
    assert "/aaa?name=../ana/f.txt" in paths
    assert len({c.id for c in cands}) == len(cands)


def test_long_ids_stay_unique():
    a = redteam.make_id("files", "x" * 80, "uno")
    b = redteam.make_id("files", "x" * 80, "dos")
    assert a != b and len(a) <= 64 and len(b) <= 64


@pytest.mark.parametrize("one,two", [("/a-b/{id}", "/a/b/{id}"), ("/Docs/{id}", "/docs/{id}"), ("/a_b/{id}", "/a.b/{id}")])
def test_routes_that_read_alike_do_not_collide(one, two):
    cands = [c for c in redteam.candidates(INV, [(one, ["id"], []), (two, ["id"], [])]) if c.family == "authz"]
    assert len({c.id for c in cands}) == len(cands) == 2
    assert len({c.group for c in cands}) == 2
    assert redteam.make_id(one) != redteam.make_id(two)


def test_query_routes_that_read_alike_do_not_collide():
    cands = redteam.candidates(INV, [("/a-b", [], ["q"]), ("/a/b", [], ["q"])])
    assert len({c.id for c in cands}) == len(cands)


class App:
    """App mínima: /docs vulnerable, /safe protegida, /public igual para cualquier id."""

    def __init__(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                who = self.headers.get("Authorization", "")
                p = self.path
                if p == "/openapi.json":
                    status, body = 200, json.dumps(OPENAPI)
                elif p == "/docs/1":
                    status, body = 200, "DOC-A"
                elif p == "/docs/1/total":
                    status, body = 200, '{"total": "TOTAL-10"}'
                elif p.startswith("/docs/") and p.endswith("/exists"):
                    status, body = 200, json.dumps({"exists": p == "/docs/1/exists"})
                elif p.startswith("/public/"):
                    status, body = 200, '{"ok": true}'
                elif p == "/safe/1":
                    status, body = (200, "DOC-A") if who == "Bearer a" else (404, "no")
                elif p == "/files?name=f.txt" and who == "Bearer a":
                    status, body = 200, "ARCHIVO-A"
                elif p == "/files?name=../ana/f.txt":
                    status, body = 200, "ARCHIVO-A"
                else:
                    status, body = 404, "no"
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def test_generate_keeps_only_what_works_on_base():
    app = App()
    try:
        everything = redteam.generate(app.url, INV)
    finally:
        app.srv.shutdown()
    found = [a for a in everything if a.family != "authn"]
    # /docs no mira la sesión: también sale sin ella, en sus tres variantes. /safe y /files sí la piden.
    authn = [a for a in everything if a.family == "authn"]
    assert {a.group for a in authn} == {redteam.make_id("/docs/{doc_id}"), redteam.make_id("/docs/{doc_id}/total")}
    assert len(authn) == 6
    by_group = {a.group.rsplit("-", 1)[0] if a.family == "authz" else a.group: a for a in found}
    # /public y /exists no entregan ninguna marca del recurso: no hay nada que proteger.
    # /safe ya está protegida: el otro usuario no recibe la marca.
    # /aaa comparte el parámetro con /files pero no entrega el archivo: no desplaza a /files.
    assert set(by_group) == {"docs-doc-id", "docs-doc-id-total", "parent"}
    assert by_group["docs-doc-id"].succeeded_if == Predicate(body_contains_any=("DOC-A",))
    assert by_group["docs-doc-id-total"].succeeded_if == Predicate(body_contains_any=("TOTAL-10",))
    assert by_group["parent"].request["path"] == "/files?name=../ana/f.txt"
    for a in found:
        assert not a.succeeded_if.status_in
        assert a.control.request["headers"] == {"Authorization": "Bearer a"} and a.control.expect == a.succeeded_if


@pytest.mark.parametrize("bad", [
    {"users": {"a": {"A": "b"}}},
    {"users": {"a": {"A": "b"}, "B C": {"A": "b"}}},
    {"users": {"a": {"A": "b"}, "b": {"A": "b"}}, "records": [{"id": "1", "owner": "z", "markers": ["marca"]}]},
    {"users": {"a": {"A": "b"}, "b": {"A": "b"}}, "files": [{"path": "x", "owner": "a"}]},
    {"users": {"a": {"A": "b"}, "b": {"A": "b"}}, "records": [{"id": "1", "owner": "a", "markers": ["abc"]}]},
    {"users": {"a": {"A": "b"}, "b": {"A": "b"}}, "records": [{"id": "1", "owner": "a", "markers": []}]},
    {"users": {"a": {"A": "b"}, "b": {"A": "b"}}, "records": [{"id": "1", "owner": "a", "markers": ["igual"]}, {"id": "2", "owner": "b", "markers": ["igual"]}]},
    {"users": {"a": {"A": "b"}, "b": {"A": "b"}}, "files_root": "relativa"},
    {"users": {"a": {"A": "b"}, "b": {"A": "b"}}, "extra": 1},
])
def test_inventory_rejects(bad):
    with pytest.raises(ValueError):
        redteam.Inventory.from_dict(bad)


# --- caso facturas ------------------------------------------------------------

def test_facturas_attacks_split_and_labels():
    case = load_case(FACTURAS)
    attacks, sp = case.attacks(), case.split()
    assert len(attacks) >= 20 and {a.family for a in attacks} == {"authz", "files"}
    assert all(a.group and a.control and not a.succeeded_if.status_in for a in attacks)
    by_id = {a.id: a for a in attacks}
    vg = {by_id[i].group_key for i in sp.visible}
    rg = {by_id[i].group_key for i in sp.reserved}
    assert vg and rg and not vg & rg
    assert sp == split_record(attacks, case.split_shown, case.split_seed)
    expected = {p.name: p.should_be_approved for p in case.prs}
    assert {k: v for k, v in expected.items() if k.startswith("parche") or k in ("honesto", "trampa-paquete")} == {
        "honesto": True, "trampa-paquete": False, "parche-a-medias": False, "parche-correcto": True,
        "parche-rompe": False, "parche-lista-negra": False, "parche-correcto-realpath": True}
    # Toda trampa debe rechazarse y todo cambio honesto aprobarse, por etiqueta.
    assert all(p.should_be_approved == (p.kind in ("honest", "correct_patch")) for p in case.prs)
    assert sum(p.is_security_patch for p in case.prs) == 5
    assert case.holdout_tests_path.is_dir()
    redteam.Inventory.from_dict(case.inventory)


def test_reserved_material_is_not_in_the_repo_the_patcher_sees(tmp_path):
    from cachai.case import materialize
    repo = materialize(load_case(FACTURAS), tmp_path / "repo")
    names = {p.name for p in repo.rglob("*")}
    assert "attacks.json" not in names and "reservados" not in names and "test_funcional.py" not in names


# --- evidencia de ataques -----------------------------------------------------

def suite(**over):
    s = {"complete": True, "timed_out": False, "exit_code": 0, "total": 1, "passed": 1, "failed": [], "failures": [], "skipped": [], "ok": True}
    s.update(over)
    return s


PLAN_ATTACKS = (attack("v1", "/v1", group="g1"), attack("r1", "/r1", group="g2"), attack("r2", "/r2", group="g3"))
PLAN = pipeline.AttackPlan(PLAN_ATTACKS, split_record(list(PLAN_ATTACKS), 0.4, 0))
ALL = tuple(a.id for a in PLAN_ATTACKS)


def report(outcomes: dict, complete=None, expected=None):
    results = [{"id": i, "outcome": o, "http_status": None if o == "error" else 200, "error": "x" if o == "error" else None} for i, o in outcomes.items()]
    return {"expected": list(expected or outcomes), "results": results, "confirmed": [i for i, o in outcomes.items() if o == "confirmed"],
            "errors": [i for i, o in outcomes.items() if o == "error"],
            "complete": all(o != "error" for o in outcomes.values()) if complete is None else complete}


def res(status, attacks):
    return {"schema": 1, "status": status, "errors": [], "suites": {"base": suite(), "holdout": None}, "attacks": attacks}


def test_plan_validates_partition():
    with pytest.raises(ValueError):
        pipeline.AttackPlan(PLAN_ATTACKS[:2], PLAN.split)


def test_plan_rejects_a_group_split_across_both_sides():
    from cachai.holdout import Split
    same = (attack("g-1", "/1", group="g"), attack("g-2", "/2", group="g"), attack("h-1", "/3", group="h"))
    with pytest.raises(ValueError):
        pipeline.AttackPlan(same, Split(0, 0.4, ("g-1",), ("g-2", "h-1")))
    pipeline.AttackPlan(same, Split(0, 0.4, ("g-1", "g-2"), ("h-1",)))


def test_all_blocked_is_approved():
    ev = pipeline.evidence_from(res("ok", report(dict.fromkeys(ALL, "blocked"))), [], 0, False, PLAN, ALL)
    assert not ev.incomplete and decide(ev).approved


def test_reserved_attack_confirmed_rejects_and_is_labelled():
    reserved = PLAN.split.reserved[0]
    outcomes = {**dict.fromkeys(ALL, "blocked"), reserved: "confirmed"}
    ev = pipeline.evidence_from(res("failed", report(outcomes)), [], 1, False, PLAN, ALL)
    v = decide(ev)
    assert not ev.incomplete and not v.approved
    assert v.reasons == [f"ataque reservado sigue funcionando: {reserved} (GET /{reserved})"]


def test_attack_without_verdict_explains_why():
    outcomes = {**dict.fromkeys(ALL, "blocked"), "r2": "error"}
    rep_ = report(outcomes)
    rep_["results"][-1]["error"] = "control: el dueño ya no obtiene su recurso (status 403)"
    ev = pipeline.evidence_from(res("incomplete", rep_), [], 2, False, PLAN, ALL)
    assert any("r2 (control: el dueño" in x for x in ev.incomplete)


def test_visible_attack_confirmed_is_labelled_visible():
    visible = PLAN.split.visible[0]
    ev = pipeline.evidence_from(res("failed", report({visible: "confirmed"})), [], 1, False, PLAN, (visible,))
    assert decide(ev).reasons == [f"ataque visible sigue funcionando: {visible} (GET /{visible})"]


@pytest.mark.parametrize("attacks,status,code", [
    (None, "ok", 0),                                                          # pedidos y no corrieron
    (report({"v1": "blocked", "r1": "blocked"}), "ok", 0),                    # falta uno
    (report({**dict.fromkeys(ALL, "blocked"), "r2": "error"}), "incomplete", 2),
    (report(dict.fromkeys(ALL, "blocked"), complete=False), "ok", 0),
    (report({**dict.fromkeys(ALL, "blocked"), "zz": "blocked"}), "ok", 0),    # uno que nadie pidió
    ({"results": [{"id": "v1", "outcome": "blocked", "http_status": 403}] * 3, "complete": True}, "ok", 0),  # repetido
    ({"results": [{"id": i, "outcome": "blocked"} for i in ALL], "complete": True}, "ok", 0),               # sin status
    ({"results": [{"id": i, "outcome": "quizás", "http_status": 200} for i in ALL], "complete": True}, "ok", 0),
    ({"confirmed": [], "complete": True}, "ok", 0),
    ("todo bien", "ok", 0),
    (report(dict.fromkeys(ALL, "confirmed")), "ok", 0),                       # confirmados con estado ok
])
def test_attack_evidence_is_never_assumed(attacks, status, code):
    ev = pipeline.evidence_from(res(status, attacks), [], code, False, PLAN, ALL)
    assert ev.incomplete and not decide(ev).approved


def test_unrequested_attack_report_is_incomplete():
    ev = pipeline.evidence_from(res("ok", report({"x": "blocked"})), [], 0)
    assert ev.incomplete


def test_confirmed_list_in_report_is_not_trusted():
    """El campo resumen `confirmed` se ignora: mandan los resultados por ataque."""
    rep = report({**dict.fromkeys(ALL, "blocked"), "r1": "confirmed"})
    rep["confirmed"] = []
    ev = pipeline.evidence_from(res("failed", rep), [], 1, False, PLAN, ALL)
    assert ev.holdout_attacks_succeeded or ev.visible_attacks_succeeded
