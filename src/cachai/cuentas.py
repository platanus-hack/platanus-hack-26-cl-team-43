"""Las cuentas del servidor: sesión con Clerk, claves del CLI, fichas subidas y el panel.

Dos formas de identificarse, las dos por `Authorization: Bearer`:

- la página manda el token de sesión de Clerk (un JWT firmado, se verifica con las claves
  públicas de la instancia: no hace falta ningún secreto en el servidor);
- el CLI manda una clave `cachai_…` creada con `cachai login`. Se guarda solo su SHA-256.

`cachai login` no pasa la clave por el navegador: el CLI inventa un verificador, abre la
página con su SHA-256 (el desafío), la persona lo aprueba con su sesión y el CLI canjea el
verificador por la clave.
"""
import hashlib
import secrets
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from cachai.db import Auditoria, AutorizacionCli, Clave, Medicion, Usuario, now

KEY_PREFIX = "cachai_"
LOGIN_TTL = timedelta(minutes=10)
MAX_BODY = 512 * 1024
MAX_AUDITS = 5000  # por usuario
MAX_KEYS = 50  # activas por usuario
MAX_ENDPOINTS = 64
PAGE_HEADERS = {"Content-Security-Policy": "frame-ancestors 'none'", "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer"}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def publishable_key(frontend_api: str) -> str:
    """La clave pública de Clerk es el dominio de su API en base64: no es un secreto."""
    import base64

    host = frontend_api.split("://", 1)[-1].rstrip("/")
    return "pk_live_" + base64.b64encode(f"{host}$".encode()).decode().rstrip("=")


def clerk_verifier(frontend_api: str, parties: set[str]) -> Callable[[str], dict]:
    """Verifica el token de sesión de Clerk contra el JWKS público de la instancia."""
    import jwt

    issuer = frontend_api.rstrip("/")
    jwks = jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True)

    def verify(token: str) -> dict:
        key = jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=issuer, leeway=5,
                            options={"require": ["exp", "iss", "sub"]})
        # azp es el origen que pidió el token: tiene que ser esta página
        if claims.get("azp") and claims["azp"] not in parties:
            raise ValueError("token emitido para otro origen")
        return claims

    return verify


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite devuelve las fechas sin zona
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _int(v, hi: int = 10**9) -> int:
    try:
        return max(0, min(hi, int(v)))
    except (TypeError, ValueError):
        return 0


def _float(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if 0 <= f < 1e9 else 0.0


def _text(v, size: int) -> str:
    return str(v if v is not None else "")[:size]


def provider_of(target: str) -> str:
    """`wafer/us` y `wafer/fp4` son del mismo proveedor; `together:MODELO` es together."""
    name = target.removesuffix(" (piso)")
    for sep in ("/", ":"):
        name = name.split(sep, 1)[0]
    return name[:120] or "?"


def rows_from(ficha: dict) -> list[dict]:
    """Una fila de mediciones por endpoint de la ficha. Lanza ValueError si no es una ficha."""
    endpoints = ficha.get("endpoints")
    if not isinstance(ficha.get("modelo"), str) or not isinstance(endpoints, list):
        raise ValueError("no es una ficha de cachai")
    if not 1 <= len(endpoints) <= MAX_ENDPOINTS:
        raise ValueError("la ficha tiene demasiados endpoints o ninguno")
    out = []
    for e in endpoints:
        if not isinstance(e, dict) or not isinstance(e.get("target"), str):
            raise ValueError("endpoint mal formado")
        role = "referencia" if e.get("is_reference") else "piso" if e.get("is_floor") else "auditado"
        hidden, lat, price = e.get("hidden_tokens"), e.get("latency_p50_ms"), e.get(
            "precio_salida_usd_millon")
        flags = e.get("flags") if isinstance(e.get("flags"), list) else []
        out.append(dict(
            endpoint=_text(e["target"], 200), proveedor=provider_of(e["target"]),
            via=_text(e.get("via"), 20),
            cuantizacion=_text(e["cuantizacion_declarada"], 40) if e.get(
                "cuantizacion_declarada") else None,
            precio_salida=_float(price) if price is not None else None,
            rol=role, distinguible=bool(e.get("distinguible")) and role == "auditado",
            pruebas=_int(e.get("n")), errores=_int(e.get("errors")),
            latencia_p50_ms=_int(lat) if lat is not None else None,
            costo_usd=_float(e.get("cost_usd")),
            tareas_ok=_int(e.get("tasks_ok")), tareas_n=_int(e.get("tasks_n")),
            iguales=_int(e.get("agree")), iguales_n=_int(e.get("agree_n")),
            libre_iguales=_int(e.get("free_agree")), libre_n=_int(e.get("free_n")),
            tools_ok=_int(e.get("tools_ok")), tools_n=_int(e.get("tools_n")),
            canarios_filtrados=_int(e.get("canary_leaks")), canarios_n=_int(e.get("canary_n")),
            tokens_ocultos=_int(hidden) if hidden is not None else None,
            motivos=[_text(x, 300) for x in flags[:20]]))
    return out


def _measured_at(ficha: dict) -> datetime:
    try:
        return datetime.strptime(str(ficha.get("fecha_utc")), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return now()


def summarize(audits: list[Auditoria], rows: list[Medicion], today: datetime) -> dict:
    """Lo que muestra el panel: el uso, y lo medido por proveedor y por modelo."""
    when = {a.id: _aware(a.medida_en) for a in audits}
    days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]
    per_day = dict.fromkeys(days, 0)
    for a in audits:
        d = when[a.id].strftime("%Y-%m-%d")
        if d in per_day:
            per_day[d] += 1
    measured = [r for r in rows if r.rol != "piso"]
    origin = defaultdict(int)
    for a in audits:
        origin[a.origen] += 1

    def group(key: str) -> list[dict]:
        groups: dict[str, list[Medicion]] = defaultdict(list)
        for r in measured:
            groups[getattr(r, key)].append(r)
        out = []
        for name, rs in groups.items():
            audited = [r for r in rs if r.rol == "auditado"]
            last = max(rs, key=lambda r: (when.get(r.auditoria_id, today), r.id))
            lats = [r.latencia_p50_ms for r in rs if r.latencia_p50_ms]
            total = lambda f: sum(getattr(r, f) for r in rs)  # noqa: E731
            out.append({
                key: name,
                "endpoints": sorted({r.endpoint for r in rs}),
                "proveedores": sorted({r.proveedor for r in rs}),
                "modelos": sorted({r.modelo for r in rs}),
                "auditorias": len({r.auditoria_id for r in rs}),
                "auditado": len(audited),
                "distinguible": sum(r.distinguible for r in audited),
                "tareas_ok": total("tareas_ok"), "tareas_n": total("tareas_n"),
                "iguales": sum(r.iguales for r in audited),
                "iguales_n": sum(r.iguales_n for r in audited),
                "tools_ok": total("tools_ok"), "tools_n": total("tools_n"),
                "canarios_filtrados": total("canarios_filtrados"),
                "canarios_n": total("canarios_n"),
                "errores": total("errores"), "pruebas": total("pruebas"),
                "latencia_p50_ms": round(statistics.median(lats)) if lats else None,
                "costo_usd": round(total("costo_usd"), 6),
                "ultima": _iso(when.get(last.auditoria_id)),
                "ultimo_resultado": ("referencia" if last.rol == "referencia" else
                                     "distinguible" if last.distinguible else "sin desviación"),
            })
        return sorted(out, key=lambda g: (-g["auditorias"], g[key]))

    by_audit: dict[int, list[Medicion]] = defaultdict(list)
    for r in measured:
        by_audit[r.auditoria_id].append(r)
    recent = sorted(audits, key=lambda a: (when[a.id], a.id), reverse=True)[:30]
    return {
        "uso": {
            "auditorias": len(audits),
            "endpoints": len({r.endpoint for r in measured}),
            "proveedores": len({r.proveedor for r in measured}),
            "modelos": len({a.modelo for a in audits}),
            "pruebas": sum(r.pruebas for r in rows),
            "distinguibles": sum(r.distinguible for r in measured),
            "gasto_usd": round(sum(a.gasto_usd for a in audits), 6),
            "ultima": _iso(max(when.values())) if when else None,
            "por_origen": dict(origin),
            "por_dia": [{"dia": d, "auditorias": n} for d, n in per_day.items()],
        },
        "proveedores": group("proveedor"),
        "modelos": group("modelo"),
        "auditorias": [{
            "id": a.id, "medida_en": _iso(when[a.id]), "modelo": a.modelo,
            "referencia": a.referencia, "juego": a.juego, "semilla": a.semilla,
            "origen": a.origen, "gasto_usd": a.gasto_usd, "segundos": a.segundos,
            "endpoints": [{"endpoint": r.endpoint, "rol": r.rol, "distinguible": r.distinguible,
                           "motivos": r.motivos} for r in by_audit[a.id]],
        } for a in recent],
    }


def router(sessions, verify: Callable[[str], dict], public_url: str,
           frontend_api: str) -> APIRouter:
    api = APIRouter()
    pages = Path(__file__).parent

    def actor(request: Request, web_only: bool = False) -> tuple[str, str]:
        """(id del usuario, origen). origen es `cli` con clave y `web` con sesión de Clerk."""
        if _int(request.headers.get("content-length")) > MAX_BODY:
            raise HTTPException(413, "cuerpo demasiado grande")
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(401, "falta iniciar sesión")
        token = header[7:].strip()
        with sessions() as s:
            if token.startswith(KEY_PREFIX):
                key = s.scalar(select(Clave).where(Clave.hash == sha(token),
                                                   Clave.revocada_en.is_(None)))
                if key is None or web_only:
                    raise HTTPException(401, "clave inválida o revocada")
                key.usada_en = now()
                s.commit()
                return key.usuario_id, "cli"
            try:
                uid = str(verify(token)["sub"])
            except Exception:
                raise HTTPException(401, "sesión inválida o vencida") from None
            user = s.get(Usuario, uid)
            if user is None:
                s.add(Usuario(id=uid))
            else:
                user.visto_en = now()
            try:
                s.commit()
            except IntegrityError:  # dos pedidos crearon el usuario a la vez
                s.rollback()
            return uid, "web"

    @api.get("/api/config")
    def config():
        return {"cuentas": True, "clerk_pk": publishable_key(frontend_api),
                "clerk_js": f"{frontend_api.rstrip('/')}/npm/@clerk/clerk-js@5/dist/"
                            "clerk.headless.browser.js",
                # el portal de cuentas de la misma instancia de Clerk
                "entrar": frontend_api.rstrip("/").replace("://clerk.", "://accounts.", 1)
                          + "/sign-in",
                "url": public_url}

    @api.post("/api/yo")
    def set_me(request: Request, body: dict = Body(default={})):
        uid, _ = actor(request, web_only=True)
        with sessions() as s:
            user = s.get(Usuario, uid)
            user.email, user.nombre = _text(body.get("email"), 320), _text(body.get("nombre"), 200)
            s.commit()
        return {"ok": True}

    @api.get("/api/yo")
    def me(request: Request):
        uid, _ = actor(request)
        with sessions() as s:
            user = s.get(Usuario, uid)
            n = s.scalar(select(func.count()).select_from(Auditoria).where(
                Auditoria.usuario_id == uid))
            spent = s.scalar(select(func.coalesce(func.sum(Auditoria.gasto_usd), 0.0)).where(
                Auditoria.usuario_id == uid))
            return {"id": uid, "email": user.email, "nombre": user.nombre, "auditorias": n,
                    "gasto_usd": round(float(spent), 6), "panel": f"{public_url}/panel"}

    @api.post("/api/cli/autorizar")
    def authorize(request: Request, body: dict = Body(...)):
        uid, _ = actor(request, web_only=True)
        challenge = str(body.get("desafio", "")).lower()
        if len(challenge) != 64 or any(c not in "0123456789abcdef" for c in challenge):
            raise HTTPException(400, "desafío inválido")
        with sessions() as s:
            s.execute(delete(AutorizacionCli).where(AutorizacionCli.expira_en < now()))
            s.merge(AutorizacionCli(desafio=challenge, usuario_id=uid,
                                    nombre=_text(body.get("nombre"), 120) or "equipo",
                                    expira_en=now() + LOGIN_TTL))
            s.commit()
        return {"ok": True}

    @api.post("/api/cli/canje")
    def redeem(body: dict = Body(...)):
        challenge = sha(str(body.get("verificador", "")))
        with sessions() as s:
            row = s.get(AutorizacionCli, challenge)
            if row is None or _aware(row.expira_en) < now():
                raise HTTPException(404, "todavía sin autorizar")
            active = s.scalar(select(func.count()).select_from(Clave).where(
                Clave.usuario_id == row.usuario_id, Clave.revocada_en.is_(None)))
            if active >= MAX_KEYS:
                raise HTTPException(429, "demasiados equipos conectados: revocá alguno en el panel")
            key = KEY_PREFIX + secrets.token_urlsafe(32)
            s.add(Clave(usuario_id=row.usuario_id, nombre=row.nombre, hash=sha(key)))
            user = s.get(Usuario, row.usuario_id)
            s.delete(row)  # un solo uso
            s.commit()
            return {"clave": key, "email": user.email, "panel": f"{public_url}/panel"}

    @api.get("/api/claves")
    def keys(request: Request):
        uid, _ = actor(request, web_only=True)
        with sessions() as s:
            rows = s.scalars(select(Clave).where(Clave.usuario_id == uid,
                                                 Clave.revocada_en.is_(None))
                             .order_by(Clave.creada_en.desc())).all()
            return {"claves": [{"id": k.id, "nombre": k.nombre, "creada_en": _iso(k.creada_en),
                                "usada_en": _iso(k.usada_en)} for k in rows]}

    @api.delete("/api/claves/actual")
    def revoke_current(request: Request):
        """`cachai logout`: revoca la clave con la que llega el pedido."""
        actor(request)
        token = request.headers["authorization"][7:].strip()
        with sessions() as s:
            key = s.scalar(select(Clave).where(Clave.hash == sha(token)))
            if key is None:
                raise HTTPException(400, "este pedido no llegó con una clave del CLI")
            key.revocada_en = now()
            s.commit()
        return {"ok": True}

    @api.delete("/api/claves/{key_id}")
    def revoke(key_id: int, request: Request):
        uid, _ = actor(request, web_only=True)
        with sessions() as s:
            key = s.scalar(select(Clave).where(Clave.id == key_id, Clave.usuario_id == uid))
            if key is None:
                raise HTTPException(404, "no existe")
            key.revocada_en = now()
            s.commit()
        return {"ok": True}

    @api.post("/api/fichas")
    def upload(request: Request, body: dict = Body(...)):
        uid, origin = actor(request)
        ficha = body.get("ficha")
        try:
            rows = rows_from(ficha if isinstance(ficha, dict) else {})
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        evidence = _text(ficha.get("evidencia_sha256"), 64)
        seed, secs = ficha.get("semilla"), body.get("segundos")
        with sessions() as s:
            same = s.scalar(select(Auditoria.id).where(Auditoria.usuario_id == uid,
                                                       Auditoria.evidencia_sha256 == evidence))
            if same is not None:  # subir dos veces la misma ficha no la duplica
                return {"id": same, "repetida": True, "panel": f"{public_url}/panel"}
            n = s.scalar(select(func.count()).select_from(Auditoria).where(
                Auditoria.usuario_id == uid))
            if n >= MAX_AUDITS:
                raise HTTPException(429, "esta cuenta llegó al máximo de auditorías guardadas")
            audit = Auditoria(
                usuario_id=uid, modelo=_text(ficha["modelo"], 200),
                referencia=_text(ficha.get("referencia"), 200),
                juego=_text(ficha.get("juego"), 20),
                semilla=_int(seed, 2**62) if seed is not None else None, origen=origin,
                version=_text(body.get("version"), 40), evidencia_sha256=evidence,
                gasto_usd=_float(ficha.get("gasto_usd")),
                segundos=_int(secs, 10**6) if secs is not None else None,
                medida_en=_measured_at(ficha), ficha=ficha)
            s.add(audit)
            s.flush()
            s.add_all(Medicion(auditoria_id=audit.id, usuario_id=uid, modelo=audit.modelo, **r)
                      for r in rows)
            s.commit()
            return {"id": audit.id, "repetida": False, "panel": f"{public_url}/panel"}

    @api.get("/api/panel")
    def panel(request: Request):
        uid, _ = actor(request)
        with sessions() as s:
            user = s.get(Usuario, uid)
            audits = s.scalars(select(Auditoria).where(Auditoria.usuario_id == uid)).all()
            rows = s.scalars(select(Medicion).where(Medicion.usuario_id == uid)).all()
            data = summarize(list(audits), list(rows), now())
            data["usuario"] = {"email": user.email, "nombre": user.nombre}
            return data

    @api.get("/panel", response_class=HTMLResponse)
    def panel_page():
        return HTMLResponse((pages / "panel.html").read_text(), headers=PAGE_HEADERS)

    @api.get("/cli", response_class=HTMLResponse)
    def cli_page():
        return HTMLResponse((pages / "cli.html").read_text(), headers=PAGE_HEADERS)

    return api
