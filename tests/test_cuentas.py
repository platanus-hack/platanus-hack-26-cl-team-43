"""Las cuentas: migración, sesión, `cachai login`, fichas subidas y el panel."""
import hashlib
import json
import os
import warnings
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from cachai import cli, cuentas, db, sesion
from cachai.serve import create_app

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

FICHA = json.loads(next(
    (Path(__file__).parent.parent / "results" / "proveedores").glob("ficha-*07-31-56z.json")
).read_text())


def fake_verify(token: str) -> dict:
    """Sesiones de mentira: `jwt-ana` es la usuaria `user_ana`."""
    if not token.startswith("jwt-"):
        raise ValueError("firma inválida")
    return {"sub": "user_" + token[4:]}


def web(name: str) -> dict:
    return {"Authorization": f"Bearer jwt-{name}"}


def fresh_database(tmp_path) -> str:
    """SQLite por defecto. Con CACHAI_TEST_DATABASE_URL, un Postgres de prueba que se vacía."""
    url = os.environ.get("CACHAI_TEST_DATABASE_URL")
    if not url:
        return f"sqlite:///{tmp_path / 'cachai.db'}"
    engine = create_engine(db.normalize(url))
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return url


@pytest.fixture
def client(tmp_path):
    app = create_app(database_url=fresh_database(tmp_path), verify=fake_verify)
    return TestClient(app, base_url="http://testserver")


def cli_key(client, name: str = "ana") -> str:
    verifier = f"verificador-de-{name}"
    challenge = hashlib.sha256(verifier.encode()).hexdigest()
    assert client.post("/api/cli/autorizar", headers=web(name),
                       json={"desafio": challenge, "nombre": "portátil"}).status_code == 200
    return client.post("/api/cli/canje", json={"verificador": verifier}).json()["clave"]


def test_la_migracion_crea_lo_mismo_que_los_modelos(tmp_path):
    url = fresh_database(tmp_path)
    db.upgrade(url)
    found = inspect(create_engine(db.normalize(url)))
    for table in db.Base.metadata.sorted_tables:
        cols = {c["name"]: c for c in found.get_columns(table.name)}
        assert set(cols) == {c.name for c in table.columns}, table.name
        for c in table.columns:
            assert cols[c.name]["nullable"] == c.nullable, f"{table.name}.{c.name}"
        indexed = {tuple(i["column_names"]) for i in found.get_indexes(table.name)}
        assert {(c.name,) for c in table.columns if c.index} <= indexed, table.name


def test_sin_sesion_no_hay_panel(client):
    assert client.get("/api/config").json()["clerk_pk"] == "pk_live_Y2xlcmsuamphdG8uY29tJA"
    assert client.get("/api/panel").status_code == 401
    assert client.get("/api/panel", headers={"Authorization": "Bearer otra-cosa"}).status_code == 401
    assert client.get("/api/panel", headers={"Authorization": "Bearer cachai_falsa"}).status_code == 401
    assert client.get("/panel").headers["x-frame-options"] == "DENY"


def test_login_del_cli_un_solo_uso(client):
    verifier = "v" * 43
    challenge = hashlib.sha256(verifier.encode()).hexdigest()
    assert client.post("/api/cli/canje", json={"verificador": verifier}).status_code == 404
    bad = client.post("/api/cli/autorizar", headers=web("ana"), json={"desafio": "corto"})
    assert bad.status_code == 400
    client.post("/api/yo", headers=web("ana"), json={"email": "ana@ejemplo.cl", "nombre": "Ana"})
    client.post("/api/cli/autorizar", headers=web("ana"),
                json={"desafio": challenge, "nombre": "portátil"})
    got = client.post("/api/cli/canje", json={"verificador": verifier}).json()
    assert got["clave"].startswith("cachai_") and got["email"] == "ana@ejemplo.cl"
    # el verificador no se puede canjear dos veces
    assert client.post("/api/cli/canje", json={"verificador": verifier}).status_code == 404
    key = {"Authorization": f"Bearer {got['clave']}"}
    assert client.get("/api/yo", headers=key).json()["email"] == "ana@ejemplo.cl"
    # una clave del CLI no puede autorizar otros equipos ni listar claves
    assert client.post("/api/cli/autorizar", headers=key,
                       json={"desafio": challenge}).status_code == 401
    assert client.get("/api/claves", headers=key).status_code == 401


def test_ficha_subida_y_panel(client):
    key = {"Authorization": f"Bearer {cli_key(client)}"}
    first = client.post("/api/fichas", headers=key, json={"ficha": FICHA, "segundos": 394})
    assert first.status_code == 200 and not first.json()["repetida"]
    assert client.post("/api/fichas", headers=key, json={"ficha": FICHA}).json()["repetida"]
    assert client.post("/api/fichas", headers=key, json={"ficha": {"x": 1}}).status_code == 400

    p = client.get("/api/panel", headers=web("ana")).json()
    uso = p["uso"]
    assert uso["auditorias"] == 1 and uso["modelos"] == 1 and uso["por_origen"] == {"cli": 1}
    assert uso["endpoints"] == 6 and uso["distinguibles"] == 0
    assert uso["pruebas"] == 7 * 52  # la segunda pasada de la referencia también gasta pruebas
    assert uso["gasto_usd"] == pytest.approx(0.158771)
    assert sum(d["auditorias"] for d in uso["por_dia"]) <= 1 and len(uso["por_dia"]) == 30
    by = {g["proveedor"]: g for g in p["proveedores"]}
    assert by["wafer"]["endpoints"] == ["wafer/fp4", "wafer/us"]
    assert (by["wafer"]["tools_ok"], by["wafer"]["tools_n"]) == (16, 16)
    assert by["z-ai"]["ultimo_resultado"] == "referencia"
    assert "z-ai/fp8 (piso)" not in {e for g in p["proveedores"] for e in g["endpoints"]}
    assert p["modelos"][0]["modelo"] == "z-ai/glm-5.3" and p["modelos"][0]["auditorias"] == 1
    assert len(p["auditorias"][0]["endpoints"]) == 6 and p["auditorias"][0]["segundos"] == 394

    # otra cuenta no ve nada de esto
    other = client.get("/api/panel", headers=web("beto")).json()
    assert other["uso"]["auditorias"] == 0 and other["proveedores"] == []


def test_revocar_la_clave(client):
    key = {"Authorization": f"Bearer {cli_key(client)}"}
    listed = client.get("/api/claves", headers=web("ana")).json()["claves"]
    assert [k["nombre"] for k in listed] == ["portátil"]
    # otra cuenta no puede revocarla
    assert client.delete(f"/api/claves/{listed[0]['id']}", headers=web("beto")).status_code == 404
    assert client.get("/api/yo", headers=key).status_code == 200
    assert client.delete(f"/api/claves/{listed[0]['id']}", headers=web("ana")).status_code == 200
    assert client.get("/api/yo", headers=key).status_code == 401
    assert client.get("/api/claves", headers=web("ana")).json()["claves"] == []


def test_token_de_otro_origen_se_rechaza(monkeypatch):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class FakeJwks:
        def __init__(self, *a, **k):
            pass

        def get_signing_key_from_jwt(self, token):
            return type("K", (), {"key": private.public_key()})()

    monkeypatch.setattr(jwt, "PyJWKClient", FakeJwks)
    verify = cuentas.clerk_verifier("https://clerk.ejemplo.cl", {"https://cachai.ejemplo.cl"})

    def token(**claims):
        base = {"sub": "user_1", "iss": "https://clerk.ejemplo.cl", "exp": 4102444800}
        return jwt.encode({**base, **claims}, private, algorithm="RS256")

    assert verify(token(azp="https://cachai.ejemplo.cl"))["sub"] == "user_1"
    with pytest.raises(ValueError):
        verify(token(azp="https://otro.ejemplo.cl"))
    with pytest.raises(jwt.InvalidTokenError):
        verify(token(iss="https://clerk.otro.cl"))
    with pytest.raises(jwt.InvalidTokenError):
        verify(token(exp=1))


def test_cachai_login_guarda_la_clave_y_sube_la_ficha(client, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("CACHAI_API_KEY", raising=False)
    said = []

    def approve(_seconds):
        # la persona abre el enlace que mostró el CLI y lo autoriza con su sesión
        url = next(l.strip() for l in "\n".join(said).splitlines() if "/cli?" in l)
        challenge = url.split("d=")[1].split("&")[0]
        assert sesion.code_of(challenge) in "\n".join(said)
        client.post("/api/cli/autorizar", headers=web("ana"), json={"desafio": challenge})

    cred = sesion.login(client, "http://testserver", browser=False, sleep=approve,
                        say=said.append)
    assert sesion.load() == cred and cred["clave"].startswith("cachai_")
    assert oct(sesion.path().stat().st_mode & 0o777) == "0o600"
    assert sesion.check(cred, client)["auditorias"] == 0
    assert sesion.upload(cred, FICHA, client, seconds=10) == "https://cachai.jjato.com/panel"
    assert sesion.check(cred, client)["auditorias"] == 1
    assert sesion.logout(client) and sesion.load() is None
    with pytest.raises(SystemExit, match="cachai login"):
        sesion.check(cred, client)  # la clave quedó revocada en el servidor


def test_scan_pide_cuenta(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("CACHAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="cachai login"):
        cli.main(["scan", "--no-openrouter", "-e", "ollama:a", "-e", "ollama:b"])


def test_sin_base_la_demo_corre_sin_cuentas(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = TestClient(create_app())
    assert c.get("/api/config").json() == {"cuentas": False}
    assert c.get("/panel").status_code == 404 and c.get("/salud").json() == {"ok": True}
    # una base que no responde no tira la demo
    c = TestClient(create_app(database_url="postgresql://nadie:x@127.0.0.1:1/nada"))
    assert c.get("/api/config").json() == {"cuentas": False}


def test_login_aguanta_un_corte_y_se_rinde_si_el_servidor_no_vuelve(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) <= 3:
            return httpx.Response(502, text="reiniciando")
        return httpx.Response(200, json={"clave": "cachai_x", "email": "ana@ejemplo.cl"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    cred = sesion.login(http, "http://s", browser=False, sleep=lambda s: None, say=lambda m: None)
    assert cred["clave"] == "cachai_x" and len(calls) == 4

    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(502)))
    with pytest.raises(SystemExit, match="respondió 502"):
        sesion.login(down, "http://s", browser=False, sleep=lambda s: None, say=lambda m: None)
