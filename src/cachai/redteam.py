"""Equipo rojo: genera variantes de ataque por plantillas y las confirma contra la base.

Solo contra apps propias dentro del sandbox. No usa ningún modelo: parte de un
inventario del caso (usuarios, registros con dueño, archivos con dueño) y de las
rutas que la app publica en su OpenAPI, y arma candidatos:

- `authz`: un usuario pide el registro de otro, en cada ruta que recibe un id
  por la ruta o por un parámetro de consulta.
- `files`: un usuario pide el archivo de otro escribiendo la ruta de varias
  formas (subir con `..`, ruta absoluta, barra codificada, etc.).
- `authn`: el mismo pedido que hace el dueño, pero sin sesión: sin headers de
  autenticación, con el header vacío y con un token inválido. También cubre
  las rutas sin parámetros (listados).
- `sqli`: un usuario pide su propio registro con una inyección SQL agregada al
  id (`' OR 1=1 -- `, comillas dobles, numérica, `LIKE`). Se confirma si la
  respuesta trae marcas de otro usuario y el mismo pedido sin inyección no las
  trae: así una falla de autorización no pasa por inyección. Con marcas
  canario (#21), ningún pedido legítimo las devuelve.
- `ssrf`: en cada parámetro de consulta con nombre de URL (`url`, `link`,
  `callback`, `webhook`, `image`...) se pasa la URL de un servidor canario que
  corre dentro del mismo contenedor del equipo rojo, en la red interna de la
  corrida. Se confirma si el canario recibe el pedido: el servidor de la app
  fue a buscar una URL que eligió quien ataca.
- `cmdi`: en cada parámetro de consulta, valores con separadores de shell que
  ejecutan `touch /tmp/cachai-cmdi-<token>`, con un token aleatorio por
  corrida. El equipo rojo solo manda las sondas; quien confirma es Cachai
  desde el host, mirando qué archivos aparecieron en el contenedor de la app
  (`sandbox.run_redteam`). El PR no controla esa inspección.
- `writes`: PUT, PATCH, POST y DELETE sobre el recurso de otro usuario, con un
  cuerpo mínimo válido tomado del esquema y una marca de escritura aleatoria
  por corrida. El dueño lee su recurso por el GET de la misma ruta antes y
  después: se confirma si aparece la marca de escritura o si sus marcas
  desaparecieron. Van al final: no ensucian los datos de los demás ataques.

Cada recurso del inventario declara sus marcas: textos que solo su dueño debería
ver. Un candidato solo se conserva si se confirma contra la app de la rama base:
el dueño recibe alguna marca en esa ruta y quien ataca (otro usuario, o nadie
en `authn`) también. El ataque
guarda las marcas que aparecieron y el pedido del dueño como control. No fija el
status: los datos de otro usuario son una fuga con cualquier código de respuesta.

Cada ataque lleva su grupo (la ruta, o la forma de escribir la ruta del
archivo), que es la unidad que se reserva.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import threading
import json
import re
import secrets
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .holdout import Attack, Clients, Control, Predicate, Unverifiable, exchange, searchable

INVENTORY_KEYS = {"users", "records", "files", "files_root"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
MIN_MARKER = 4  # una marca más corta aparecería por casualidad

# Formas de pedir el archivo de otro usuario desde la carpeta propia.
# {rel}: ruta del archivo desde la raíz de adjuntos; {me}: carpeta de quien ataca; {root}: raíz absoluta.
FILE_TEMPLATES: tuple[tuple[str, str, bool], ...] = (
    # (grupo, plantilla, ya viene codificada)
    ("parent", "../{rel}", False),
    ("dot-parent", "./../{rel}", False),
    ("own-dir-parent", "../{me}/../{rel}", False),
    ("double-slash", "..//{rel}", False),
    ("absolute", "{root}/{rel}", False),
    ("encoded-slash", "..%2F{rel_encoded}", True),
    ("encoded-dots", "%2e%2e/{rel}", True),
)


@dataclass(frozen=True)
class Inventory:
    users: dict[str, dict[str, str]]  # nombre -> headers de autenticación
    records: tuple[dict, ...] = ()  # {"id", "owner", "markers"}
    files: tuple[dict, ...] = ()  # {"path", "owner", "markers"}
    files_root: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Inventory":
        if not isinstance(d, dict) or set(d) - INVENTORY_KEYS:
            raise ValueError("inventario inválido")
        users = d.get("users")
        if not isinstance(users, dict) or len(users) < 2:
            raise ValueError("el inventario necesita al menos dos usuarios")
        for name, headers in users.items():
            if not NAME_RE.match(str(name)) or not isinstance(headers, dict) or not headers:
                raise ValueError(f"usuario inválido en el inventario: {name!r}")
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
                raise ValueError(f"headers inválidos para {name!r}")
        records, files = d.get("records", []), d.get("files", [])
        all_markers: list[str] = []
        for items, key in ((records, "id"), (files, "path")):
            if not isinstance(items, list):
                raise ValueError("records y files deben ser listas")
            for it in items:
                if not isinstance(it, dict) or set(it) != {key, "owner", "markers"}:
                    raise ValueError(f"entrada de inventario inválida: {it!r}")
                markers = it["markers"]
                if it["owner"] not in users or not isinstance(it[key], str) or not it[key]:
                    raise ValueError(f"entrada de inventario inválida: {it!r}")
                if not isinstance(markers, list) or not markers or not all(isinstance(m, str) and len(m) >= MIN_MARKER for m in markers):
                    raise ValueError(f"marcas inválidas (mínimo {MIN_MARKER} caracteres): {it!r}")
                all_markers += markers
        if len(all_markers) != len(set(all_markers)):
            raise ValueError("una misma marca no puede pertenecer a dos recursos")
        root = d.get("files_root", "")
        if not isinstance(root, str) or (root and not root.startswith("/")):
            raise ValueError("files_root debe ser una ruta absoluta")
        return cls(dict(users), tuple(records), tuple(files), root.rstrip("/"))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def make_id(*parts: str) -> str:
    """Id legible y único. Termina en un hash de las partes originales, porque el
    texto legible pierde información (`/a-b/{id}` y `/a/b/{id}` se leen igual)."""
    digest = hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:8]
    return f"{slug('-'.join(parts))[:55].rstrip('-')}-{digest}"


def get_routes(openapi: dict) -> list[tuple[str, list[str], list[str]]]:
    """Rutas GET de la app: (plantilla, parámetros de ruta, parámetros de consulta)."""
    routes = []
    for template, item in sorted((openapi.get("paths") or {}).items()):
        op = item.get("get") if isinstance(item, dict) else None
        if not isinstance(op, dict):
            continue
        params = [p for p in op.get("parameters", []) if isinstance(p, dict)]
        routes.append((
            template,
            [p["name"] for p in params if p.get("in") == "path"],
            [p["name"] for p in params if p.get("in") == "query"],
        ))
    return routes


WRITE_METHODS = ("put", "patch", "post", "delete")


def get_write_routes(openapi: dict) -> list[tuple[str, str, list[str], dict | None]]:
    """Rutas de escritura con id en la ruta y GET en la misma plantilla (por donde el dueño lee):
    (método, plantilla, parámetros de ruta, esquema del cuerpo JSON o None)."""
    out = []
    for template, item in sorted((openapi.get("paths") or {}).items()):
        if not isinstance(item, dict) or not isinstance(item.get("get"), dict):
            continue
        for method in WRITE_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params = [p for p in op.get("parameters", []) if isinstance(p, dict)]
            path_params = [p["name"] for p in params if p.get("in") == "path"]
            if not path_params:
                continue
            schema = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
            out.append((method.upper(), template, path_params, schema))
    return out


def example_body(schema: dict | None, components: dict, marker: str, depth: int = 0) -> object:
    """Cuerpo mínimo válido para `schema`: cada texto lleva `marker` para verlo después."""
    if not isinstance(schema, dict) or depth > 6:
        return marker
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return example_body((components.get("schemas") or {}).get(name), components, marker, depth + 1)
    for key in ("allOf", "anyOf", "oneOf"):
        if schema.get(key):
            return example_body(schema[key][0], components, marker, depth + 1)
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        return {k: example_body(v, components, marker, depth + 1) for k, v in props.items()}
    if t == "array":
        return [example_body(schema.get("items"), components, marker, depth + 1)]
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return True
    return marker


@dataclass(frozen=True)
class Candidate:
    id: str
    family: str
    group: str
    request: dict  # pedido de quien ataca
    owner_request: dict  # el pedido equivalente hecho por el dueño
    markers: tuple[str, ...]


def _fill(template: str, names: list[str], value: str) -> str:
    for n in names:
        template = template.replace("{" + n + "}", urllib.parse.quote(value, safe=""))
    return template


# Inyección SQL: se agrega al id propio de quien ataca. Cada forma es un grupo.
SQLI_PAYLOADS: tuple[tuple[str, str], ...] = (
    ("quote-or-comment", "' OR 1=1 -- "),
    ("quote-or", "' OR '1'='1"),
    ("dquote-or", '" OR "1"="1'),
    ("numeric-or", " OR 1=1"),
    ("like-or-comment", "%' OR 1=1 -- "),
)

# SSRF: parámetros cuyo nombre sugiere una URL.
SSRF_PARAM = re.compile(r"(url|uri|link|href|src|source|target|dest|redirect|next|callback|webhook|hook|image|img|"
                        r"avatar|icon|feed|endpoint|host|fetch|proxy|site|domain|remote|download|import)", re.IGNORECASE)
CANARY_PORT = 8999
CANARY_WAIT = 3.0  # segundos que se espera a que la app llame al canario


class Canary:
    """Servidor HTTP mínimo que anota las rutas que recibe. Es el testigo de SSRF."""

    def __init__(self, host: str = "0.0.0.0", port: int = CANARY_PORT) -> None:
        hits = self.hits = set()

        class Handler(http.server.BaseHTTPRequestHandler):
            def _hit(self):
                hits.add(self.path.split("?", 1)[0])
                self.send_response(200)
                self.send_header("content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")

            do_GET = do_POST = do_HEAD = do_PUT = _hit

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer((host, port), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def seen(self, token: str, wait: float = CANARY_WAIT) -> bool:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if f"/ssrf/{token}" in self.hits:
                return True
            time.sleep(0.05)
        return f"/ssrf/{token}" in self.hits

    def close(self) -> None:
        self.server.shutdown()


# Inyección de comandos: separadores de shell. {f} es el archivo testigo.
CMDI_PAYLOADS: tuple[tuple[str, str], ...] = (
    ("semicolon", "x; touch {f} #"),
    ("and", "x && touch {f} #"),
    ("pipe", "x | touch {f} #"),
    ("subshell", "$(touch {f})"),
    ("backtick", "`touch {f}`"),
    ("newline", "x\ntouch {f}\n"),
)
CMDI_DIR = "/tmp"
CMDI_PREFIX = "cachai-cmdi-"


def cmdi_probes(inv: Inventory, routes: list[tuple[str, list[str], list[str]]], nonce: str) -> list[dict]:
    """Sondas de inyección de comandos: cada una deja un archivo distinto si el comando corre.

    El id es estable entre corridas (sirve para comparar base y PR); el nombre del
    archivo lleva `nonce`, aleatorio por corrida.
    """
    attacker = sorted(inv.users)[0]
    out = []
    for template, path_params, query_params in routes:
        if path_params:
            continue  # el archivo testigo lleva barras: no entra en un tramo de ruta
        for q in query_params:
            for group, payload in CMDI_PAYLOADS:
                pid = make_id("cmdi", template, q, group)
                name = f"{CMDI_PREFIX}{pid[-8:]}-{nonce}"
                value = payload.format(f=f"{CMDI_DIR}/{name}")
                out.append({"id": pid, "group": group, "file": name, "request": {
                    "method": "GET", "path": f"{template}?{q}={urllib.parse.quote(value, safe='')}", "headers": inv.users[attacker]}})
    return out


def send_probes(base_url: str, probes: list[dict], deadline: float) -> None:
    """Manda las sondas; la respuesta no importa (el testigo es el archivo)."""
    import httpx

    with Clients() as clients:
        for pr in probes:
            if time.monotonic() > deadline:
                raise TimeoutError("presupuesto agotado mandando sondas de inyección de comandos")
            try:
                exchange(clients.for_request(pr["request"]), base_url, pr["request"], deadline)
            except (httpx.TransportError, Unverifiable):
                pass


def cmdi_attacks(probes: list[dict], present: set[str]) -> list[Attack]:
    """Las sondas cuyo archivo apareció en el contenedor de la app."""
    return [Attack(pr["id"], "cmdi", pr["request"], Predicate(status_in=tuple(range(100, 600))), pr["group"])
            for pr in probes if pr["file"] in present]


INVALID_TOKEN = "cachai-token-invalido"
# Esquemas de Authorization que se conservan al borrar o falsear la credencial.
AUTH_SCHEME_RE = re.compile(r"^(Bearer|Basic|Token|ApiKey|Digest)\s", re.IGNORECASE)


def anonymous_headers(inv: Inventory) -> dict[str, dict[str, str]]:
    """Las tres formas de pedir sin sesión válida: sin la credencial, con la
    credencial vacía y con una que no existe.

    Credencial es todo header que cambia de un usuario a otro; los que todos
    comparten (un tenant, una versión de API) se mandan igual, para que la app
    llegue a mirar la sesión. De la credencial solo se conserva un esquema
    conocido (`Bearer`); nada más del valor de un usuario real, que daría una
    sesión válida (p. ej. una cookie con varios campos).
    """
    keys = sorted({k for headers in inv.users.values() for k in headers})
    values = {k: {headers.get(k) for headers in inv.users.values()} for k in keys}
    common = {k: next(iter(v)) for k, v in values.items() if len(v) == 1 and None not in v}
    out: dict[str, dict[str, str]] = {"none": dict(common), "empty": dict(common), "invalid": dict(common)}
    for k in keys:
        if k in common:
            continue
        m = next((AUTH_SCHEME_RE.match(v) for v in values[k] if v and AUTH_SCHEME_RE.match(v)), None)
        scheme = m.group(1) if m else ""
        # Sin espacio al final: un header que termina en espacio no es legal y httpx no lo envía.
        out["empty"][k] = scheme
        out["invalid"][k] = f"{scheme} {INVALID_TOKEN}" if scheme else INVALID_TOKEN
    return out


def candidates(inv: Inventory, routes: list[tuple[str, list[str], list[str]]],
               families: tuple[str, ...] | None = None, canary_base: str | None = None,
               write_routes: list | None = None, components: dict | None = None, write_marker: str = "") -> list[Candidate]:
    """Candidatos de las familias pedidas (todas si `families` es None)."""
    out: list[Candidate] = []
    wanted = set(families) if families is not None else None
    seen: set[str] = set()
    anonymous = anonymous_headers(inv)

    def push(cid: str, family: str, group: str, path: str, headers: dict, owner_path: str, owner: str, markers) -> None:
        if cid in seen:
            raise ValueError(f"id de candidato repetido: {cid}")
        seen.add(cid)
        if wanted is not None and family not in wanted:
            return
        out.append(Candidate(
            cid, family, group,
            {"method": "GET", "path": path, "headers": headers},
            {"method": "GET", "path": owner_path, "headers": inv.users[owner]},
            tuple(markers),
        ))

    def add(family: str, group: str, id_parts: tuple[str, ...], path: str, owner_path: str, owner: str, attacker: str, markers: list[str]) -> None:
        push(make_id(*id_parts), family, group, path, inv.users[attacker], owner_path, owner, markers)

    def add_authn(group: str, id_parts: tuple[str, ...], path: str, owner: str, markers) -> None:
        """Mismo pedido que hace el dueño, sin sesión válida. Agrupado por ruta."""
        for variant, headers in anonymous.items():
            push(make_id("authn", *id_parts, variant), "authn", group, path, headers, path, owner, markers)

    owned_markers = {u: [m for it in (*inv.records, *inv.files) if it["owner"] == u for m in it["markers"]] for u in inv.users}

    for template, path_params, query_params in routes:
        # authn: la ruta pedida sin sesión entrega algo que solo ve su dueño.
        if path_params:
            for rec in inv.records:
                add_authn(make_id(template), (template, rec["id"]), _fill(template, path_params, rec["id"]), rec["owner"], rec["markers"])
        elif query_params:
            for q in query_params:
                for rec in inv.records:
                    path = f"{template}?{q}={urllib.parse.quote(rec['id'], safe='')}"
                    add_authn(make_id(template, q), (template, q, rec["id"]), path, rec["owner"], rec["markers"])
                for f in inv.files:
                    # Como el control de `files`: el nombre que el dueño usa desde su carpeta.
                    own_name = urllib.parse.quote(f["path"].split("/", 1)[-1], safe="/.")
                    add_authn(make_id(template, q), (template, q, "file", f["path"]), f"{template}?{q}={own_name}", f["owner"], f["markers"])
        else:
            # Ruta sin parámetros (un listado): lo que cada usuario ve con su sesión.
            for user in sorted(inv.users):
                if owned_markers[user]:
                    add_authn(make_id(template), (template, user), template, user, owned_markers[user])

        # sqli: quien ataca pide con su propio id más una inyección; el control es el
        # mismo pedido sin inyección, que no debe traer datos ajenos.
        if wanted is None or "sqli" in wanted:
            params = [("path", n) for n in path_params] if path_params else [("query", q) for q in query_params]
            for kind, name in params:
                for attacker in sorted(inv.users):
                    mine = [r for r in inv.records if r["owner"] == attacker]
                    theirs = [m for r in inv.records if r["owner"] != attacker for m in r["markers"]]
                    if not mine or not theirs:
                        continue
                    own_id = mine[0]["id"]
                    for group, payload in SQLI_PAYLOADS:
                        def at(value: str) -> str:
                            if kind == "path":
                                return _fill(template, path_params, value)
                            return f"{template}?{name}={urllib.parse.quote(value, safe='')}"
                        cid = make_id("sqli", template, name, group, attacker)
                        if cid in seen:
                            raise ValueError(f"id de candidato repetido: {cid}")
                        seen.add(cid)
                        out.append(Candidate(
                            cid, "sqli", group,
                            {"method": "GET", "path": at(own_id + payload), "headers": inv.users[attacker]},
                            {"method": "GET", "path": at(own_id), "headers": inv.users[attacker]},
                            tuple(theirs),
                        ))

        # ssrf: la URL del canario en cada parámetro con nombre de URL, como el primer usuario.
        if canary_base and (wanted is None or "ssrf" in wanted):
            attacker = sorted(inv.users)[0]
            for q in query_params:
                if not SSRF_PARAM.search(q):
                    continue
                cid = make_id("ssrf", template, q)
                if cid in seen:
                    raise ValueError(f"id de candidato repetido: {cid}")
                seen.add(cid)
                url = f"{canary_base}/ssrf/{cid}"
                request = {"method": "GET", "path": f"{template}?{q}={urllib.parse.quote(url, safe='')}", "headers": inv.users[attacker]}
                out.append(Candidate(cid, "ssrf", make_id(template, q), request, request, (cid,)))

        route = template
        for rec in inv.records:
            for attacker in sorted(inv.users):
                if attacker == rec["owner"]:
                    continue
                if path_params:
                    path = _fill(template, path_params, rec["id"])
                    add("authz", make_id(route), ("authz", route, rec["id"], attacker), path, path, rec["owner"], attacker, rec["markers"])
                else:
                    for q in query_params:
                        path = f"{template}?{q}={urllib.parse.quote(rec['id'], safe='')}"
                        add("authz", make_id(route, q), ("authz", route, q, rec["id"], attacker), path, path, rec["owner"], attacker, rec["markers"])
        if path_params:
            continue
        for f in inv.files:
            rel = f["path"]
            own_name = urllib.parse.quote(rel.split("/", 1)[-1], safe="/.")
            for attacker in sorted(inv.users):
                if attacker == f["owner"]:
                    continue
                for q in query_params:
                    for group, pattern, encoded in FILE_TEMPLATES:
                        if "{root}" in pattern and not inv.files_root:
                            continue
                        value = pattern.format(rel=rel, me=attacker, root=inv.files_root, rel_encoded=rel.replace("/", "%2F"))
                        if not encoded:
                            value = urllib.parse.quote(value, safe="/.")
                        add("files", group, ("files", route, q, group, rel, attacker), f"{template}?{q}={value}",
                            f"{template}?{q}={own_name}", f["owner"], attacker, f["markers"])

    # writes: al final, porque cambian datos. PUT y PATCH antes que DELETE.
    if write_routes and write_marker and (wanted is None or "writes" in wanted):
        order = {"PUT": 0, "PATCH": 1, "POST": 2, "DELETE": 3}
        for method, template, path_params, schema in sorted(write_routes, key=lambda r: (order[r[0]], r[1])):
            for rec in inv.records:
                for attacker in sorted(inv.users):
                    if attacker == rec["owner"]:
                        continue
                    cid = make_id("writes", method, template, rec["id"], attacker)
                    if cid in seen:
                        raise ValueError(f"id de candidato repetido: {cid}")
                    seen.add(cid)
                    path = _fill(template, path_params, rec["id"])
                    request = {"method": method, "path": path, "headers": inv.users[attacker]}
                    if method != "DELETE":
                        request["json"] = example_body(schema, components or {}, write_marker)
                    out.append(Candidate(cid, "writes", make_id(method, template), request,
                                         {"method": "GET", "path": path, "headers": inv.users[rec["owner"]]},
                                         tuple(rec["markers"]) + (write_marker,)))
    return out


def confirm(base_url: str, cands: list[Candidate], budget: float = 60.0, canary: Canary | None = None) -> list[Attack]:
    """Conserva los candidatos que funcionan contra la app.

    El dueño debe recibir alguna marca del recurso en esa ruta; si no, la ruta no
    entrega ese recurso y no hay nada que proteger. Con las marcas que el dueño
    sí recibe se arma el predicado, y el candidato queda si quien ataca
    también las recibe. Cada identidad (también la anónima) pide con su propio
    cliente. El pedido del dueño se hace una sola vez por ruta y usuario.
    """
    import httpx

    deadline = time.monotonic() + budget
    confirmed = []
    owner_texts: dict[str, str] = {}
    with Clients() as clients:
        for c in cands:
            if time.monotonic() > deadline:
                raise TimeoutError("presupuesto agotado confirmando candidatos")
            if c.family == "writes":
                found = _confirm_write(clients, base_url, c, deadline)
                if found:
                    confirmed.append(found)
                continue
            if c.family == "ssrf":
                if canary is not None and _confirm_ssrf(clients, base_url, c, canary, deadline):
                    # El canario es el testigo; el predicado (cualquier respuesta) solo deja el pedido registrado.
                    confirmed.append(Attack(c.id, c.family, c.request, Predicate(status_in=tuple(range(100, 600))), c.group))
                continue
            if c.family == "sqli":
                found = _confirm_sqli(clients, base_url, c, deadline)
                if found:
                    confirmed.append(found)
                continue
            try:
                key = json.dumps(c.owner_request, sort_keys=True)
                if key not in owner_texts:
                    owner_hops, _ = exchange(clients.for_request(c.owner_request), base_url, c.owner_request, deadline)
                    owner_texts[key] = "\n".join(searchable(body) for _, body in owner_hops)
                owner_text = owner_texts[key]
                present = tuple(m for m in c.markers if m in owner_text)
                if not present:
                    continue
                leak = Predicate(body_contains_any=present)
                hops, _ = exchange(clients.for_request(c.request), base_url, c.request, deadline)
            except (httpx.TransportError, Unverifiable):
                # La app no respondió algo legible a este pedido: no hay fuga que confirmar.
                continue
            if any(leak.matches(status, searchable(body)) for status, body in hops):
                confirmed.append(Attack(c.id, c.family, c.request, leak, c.group, Control(c.owner_request, leak)))
    return sorted(confirmed, key=lambda a: a.id)


def _confirm_write(clients, base_url: str, c: Candidate, deadline: float) -> Attack | None:
    """El dueño lee antes y después del pedido del atacante. Confirmado si después aparece
    la marca de escritura (la última de `markers`) o desaparecieron las marcas del dueño."""
    import httpx

    *owned, written = c.markers

    def owner_text() -> str:
        hops, _ = exchange(clients.for_request(c.owner_request), base_url, c.owner_request, deadline)
        return "\n".join(searchable(body) for _, body in hops)

    try:
        before = owner_text()
        if not any(m in before for m in owned) or written in before:
            return None  # el dueño no ve su recurso por esta ruta: no hay estado que comparar
        exchange(clients.for_request(c.request), base_url, c.request, deadline)
        after = owner_text()
    except (httpx.TransportError, Unverifiable):
        return None
    if written in after or not any(m in after for m in owned):
        return Attack(c.id, c.family, c.request, Predicate(status_in=tuple(range(100, 600))), c.group)
    return None


def _confirm_ssrf(clients, base_url: str, c: Candidate, canary: Canary, deadline: float) -> bool:
    """Confirmado si el canario recibe la ruta con el token del candidato. La
    respuesta de la app no importa: puede fallar después de hacer el pedido."""
    import httpx

    try:
        exchange(clients.for_request(c.request), base_url, c.request, deadline)
    except (httpx.TransportError, Unverifiable):
        pass
    return canary.seen(c.markers[0], wait=min(CANARY_WAIT, max(0.0, deadline - time.monotonic())))


def _confirm_sqli(clients, base_url: str, c: Candidate, deadline: float) -> Attack | None:
    """Inyección confirmada: el pedido inyectado trae marcas de otro usuario y el
    mismo pedido sin inyección no las trae (si las trae, es otra falla, no esta)."""
    import httpx

    try:
        clean, _ = exchange(clients.for_request(c.owner_request), base_url, c.owner_request, deadline)
        clean_text = "\n".join(searchable(body) for _, body in clean)
        if any(m in clean_text for m in c.markers):
            return None
        hops, _ = exchange(clients.for_request(c.request), base_url, c.request, deadline)
    except (httpx.TransportError, Unverifiable):
        return None
    text = "\n".join(searchable(body) for _, body in hops)
    present = tuple(m for m in c.markers if m in text)
    if not present:
        return None
    return Attack(c.id, c.family, c.request, Predicate(body_contains_any=present), c.group)


def generate(base_url: str, inv: Inventory, budget: float = 60.0, families: tuple[str, ...] | None = None,
             canary: Canary | None = None, canary_host: str | None = None,
             static_routes: list[dict] | None = None, info: dict | None = None) -> list[Attack]:
    """Genera y confirma candidatos. Las rutas salen del OpenAPI de la app unido con
    `static_routes` (leídas del código, `routes.extract`). En `info` deja las rutas
    del código que el OpenAPI no publica."""
    from .routes import merge

    deadline = time.monotonic() + budget
    with Clients() as clients:
        request = {"method": "GET", "path": "/openapi.json"}
        hops, _ = exchange(clients.for_request(request), base_url, request, deadline)
    status, body = hops[-1]
    if status == 200:
        openapi = json.loads(body)
    elif static_routes:
        openapi = {}
    else:
        raise RuntimeError(f"la app no publica /openapi.json (status {status}) y no hay rutas leídas del código")
    routes, hidden = merge(get_routes(openapi), static_routes or [])
    if info is not None:
        info["hidden_routes"] = hidden if openapi else []
        info["openapi"] = bool(openapi)
    canary_base = f"http://{canary_host}:{canary.port}" if canary is not None and canary_host else None
    marker = f"CNR-WRITE-{secrets.token_hex(6)}"  # aleatoria por corrida: el PR no la conoce
    cands = candidates(inv, routes, families, canary_base, get_write_routes(openapi),
                       openapi.get("components") or {}, marker)
    return confirm(base_url, cands, deadline - time.monotonic(), canary)


MAX_PROBE_BODY = 2_000_000


def probe(base_url: str, users: dict[str, dict[str, str]], budget: float = 30.0) -> dict:
    """Lo que `cachai init` necesita para proponer un inventario: el OpenAPI de la
    app y, por cada ruta GET sin parámetros, la respuesta JSON que ve cada usuario.

    Solo pedidos GET con los headers que dio quien configura; nada se escribe.
    """
    deadline = time.monotonic() + budget
    samples: dict[str, dict[str, object]] = {}
    with Clients() as clients:
        request = {"method": "GET", "path": "/openapi.json"}
        hops, _ = exchange(clients.for_request(request), base_url, request, deadline)
        status, body = hops[-1]
        if status != 200:
            raise RuntimeError(f"la app no publica /openapi.json (status {status})")
        openapi = json.loads(body)
        for template, path_params, query_params in get_routes(openapi):
            if path_params or query_params:
                continue
            for name, headers in sorted(users.items()):
                req = {"method": "GET", "path": template, "headers": headers}
                try:
                    hops, _ = exchange(clients.for_request(req), base_url, req, deadline)
                except Exception:  # una ruta que no responde no aporta al borrador
                    continue
                st, raw = hops[-1]
                if st != 200 or len(raw) > MAX_PROBE_BODY:
                    continue
                try:
                    samples.setdefault(template, {})[name] = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    return {"openapi": openapi, "samples": samples}


def main(argv: list[str] | None = None) -> int:
    """Corre dentro del contenedor de Cachai, contra la app de la rama base."""
    from .evaluator import wait_for

    p = argparse.ArgumentParser(prog="cachai-redteam")
    p.add_argument("--base-url", required=True)
    p.add_argument("--health", default="/health")
    p.add_argument("--inventory", required=True)
    p.add_argument("--budget", type=float, default=60.0)
    p.add_argument("--families", default="", help="familias a generar, separadas por coma (todas si se omite)")
    p.add_argument("--canary-host", default="", help="nombre con el que la app llega a este contenedor (activa ssrf)")
    p.add_argument("--cmdi-nonce", default="", help="parte aleatoria de los archivos testigo (activa cmdi)")
    p.add_argument("--routes", default="", help="JSON con las rutas leídas del código (routes.extract)")
    p.add_argument("--probe", action="store_true",
                   help="no ataca: devuelve el OpenAPI y los listados de cada usuario (--inventory solo necesita users)")
    args = p.parse_args(argv)
    if args.probe:
        users = json.loads(Path(args.inventory).read_text()).get("users") or {}
        if not wait_for(args.base_url.rstrip("/") + args.health, time.monotonic() + min(30.0, args.budget)):
            print(json.dumps({"error": "la app no respondió"}))
            return 2
        print(json.dumps(probe(args.base_url, users, args.budget), ensure_ascii=False))
        return 0
    inv = Inventory.from_dict(json.loads(Path(args.inventory).read_text()))
    if not wait_for(args.base_url.rstrip("/") + args.health, time.monotonic() + min(30.0, args.budget)):
        print(json.dumps({"error": "la app no respondió"}))
        return 2
    families = tuple(f for f in args.families.split(",") if f) or None
    canary = Canary() if args.canary_host and (families is None or "ssrf" in families) else None
    deadline = time.monotonic() + args.budget
    static = json.loads(Path(args.routes).read_text()) if args.routes else None
    info: dict = {}
    try:
        attacks = generate(args.base_url, inv, args.budget, families, canary, args.canary_host or None, static, info)
    finally:
        if canary is not None:
            canary.close()
    probes = []
    if args.cmdi_nonce and (families is None or "cmdi" in families):
        # Las rutas se leen de nuevo: generate no las devuelve.
        from .routes import merge
        with Clients() as clients:
            req = {"method": "GET", "path": "/openapi.json"}
            hops, _ = exchange(clients.for_request(req), args.base_url, req, deadline)
        openapi = json.loads(hops[-1][1]) if hops[-1][0] == 200 else {}
        probes = cmdi_probes(inv, merge(get_routes(openapi), static or [])[0], args.cmdi_nonce)
        send_probes(args.base_url, probes, deadline)
    print(json.dumps({"attacks": [a.to_dict() for a in attacks], "cmdi_probes": probes,
                      "hidden_routes": info.get("hidden_routes", [])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
