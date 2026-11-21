"""Ataques y tests reservados que el parchador nunca ve.

Por cada falla se generan varias variantes de ataque. Una parte se entrega al
parchador y el resto queda aquí, junto con tests funcionales, para comprobar que
el parche arregla la falla y no solo el caso que vio.

Un ataque se confirma con un predicado estructurado sobre la respuesta HTTP,
nunca ejecutando código ni preguntándole a un modelo.

Semántica del pedido: `path` es relativo al origen de la app (empieza con una
sola `/`, sin esquema ni host) y las redirecciones no se siguen: se evalúa la
primera respuesta.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

FAMILIES = ("authz", "files")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
ATTACK_KEYS = {"id", "family", "request", "succeeded_if"}
REQUEST_KEYS = {"method", "path", "headers", "json"}
PREDICATE_KEYS = {"status_in", "body_contains", "body_not_contains"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _str_list(d: dict, key: str) -> tuple[str, ...]:
    v = d.get(key, [])
    if not isinstance(v, list) or not all(isinstance(s, str) and s for s in v):
        raise ValueError(f"{key} debe ser una lista de textos no vacíos")
    return tuple(v)


@dataclass(frozen=True)
class Predicate:
    """Condición determinista que confirma que un ataque funcionó.

    Se cumplen todas las partes presentes: el status está en `status_in` y el
    cuerpo contiene cada texto de `body_contains` y ninguno de `body_not_contains`.
    """

    status_in: tuple[int, ...] = ()
    body_contains: tuple[str, ...] = ()
    body_not_contains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not (self.status_in or self.body_contains):
            raise ValueError("el predicado necesita status_in o body_contains")
        for s in self.status_in:
            if isinstance(s, bool) or not isinstance(s, int) or not 100 <= s <= 599:
                raise ValueError(f"status inválido: {s!r}")

    def matches(self, status: int, body: str) -> bool:
        if self.status_in and status not in self.status_in:
            return False
        if any(s not in body for s in self.body_contains):
            return False
        return not any(s in body for s in self.body_not_contains)

    @classmethod
    def from_dict(cls, d: dict) -> "Predicate":
        if not isinstance(d, dict):
            raise ValueError("el predicado debe ser un objeto")
        unknown = set(d) - PREDICATE_KEYS
        if unknown:
            raise ValueError(f"claves desconocidas en el predicado: {sorted(unknown)}")
        status = d.get("status_in", [])
        if not isinstance(status, list):
            raise ValueError("status_in debe ser una lista")
        return cls(
            status_in=tuple(status),
            body_contains=_str_list(d, "body_contains"),
            body_not_contains=_str_list(d, "body_not_contains"),
        )

    def to_dict(self) -> dict:
        d: dict = {}
        if self.status_in:
            d["status_in"] = list(self.status_in)
        if self.body_contains:
            d["body_contains"] = list(self.body_contains)
        if self.body_not_contains:
            d["body_not_contains"] = list(self.body_not_contains)
        return d


def validate_request(req: dict) -> None:
    if not isinstance(req, dict):
        raise ValueError("el pedido debe ser un objeto")
    unknown = set(req) - REQUEST_KEYS
    if unknown:
        raise ValueError(f"claves desconocidas en el pedido: {sorted(unknown)}")
    if req.get("method") not in METHODS:
        raise ValueError(f"método inválido: {req.get('method')!r}")
    path = req.get("path")
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise ValueError(f"ruta inválida (debe ser relativa al origen): {path!r}")
    if any(c in path for c in "\r\n\t ") or "://" in path.split("?", 1)[0]:
        raise ValueError(f"ruta inválida: {path!r}")
    headers = req.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ValueError("headers debe ser un objeto de textos")
    if any(k.lower() == "host" for k in headers):
        raise ValueError("no se permite fijar Host")


@dataclass(frozen=True)
class Attack:
    id: str
    family: str  # "authz" (datos de otro usuario) o "files" (acceso indebido a archivos)
    request: dict  # pedido HTTP: method, path, headers, json (opcional)
    succeeded_if: Predicate

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not ID_RE.match(self.id):
            raise ValueError(f"id de ataque inválido: {self.id!r}")
        if self.family not in FAMILIES:
            raise ValueError(f"familia desconocida: {self.family}")
        validate_request(self.request)
        if not isinstance(self.succeeded_if, Predicate):
            raise ValueError("succeeded_if debe ser un Predicate")

    @classmethod
    def from_dict(cls, d: dict) -> "Attack":
        if not isinstance(d, dict):
            raise ValueError("el ataque debe ser un objeto")
        unknown = set(d) - ATTACK_KEYS
        missing = ATTACK_KEYS - set(d)
        if unknown or missing:
            raise ValueError(f"ataque con claves inválidas: sobran {sorted(unknown)}, faltan {sorted(missing)}")
        return cls(
            id=d["id"],
            family=d["family"],
            request=dict(d["request"]) if isinstance(d["request"], dict) else d["request"],
            succeeded_if=Predicate.from_dict(d["succeeded_if"]),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "family": self.family,
            "request": self.request,
            "succeeded_if": self.succeeded_if.to_dict(),
        }


def load_attacks(path: str | Path) -> list[Attack]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise ValueError("el archivo de ataques debe ser una lista")
    attacks = [Attack.from_dict(d) for d in data]
    ids = [a.id for a in attacks]
    if len(ids) != len(set(ids)):
        raise ValueError("ids de ataque repetidos")
    return attacks


# --- Selección visible / reservada ---------------------------------------


@dataclass(frozen=True)
class Split:
    """Qué ataques ve el parchador y cuáles quedan reservados. Va en la evidencia."""

    seed: int
    shown_ratio: float
    visible: tuple[str, ...]
    reserved: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"seed": self.seed, "shown_ratio": self.shown_ratio, "visible": list(self.visible), "reserved": list(self.reserved)}


def split(attacks: list[Attack], shown: float = 0.4, seed: int = 0) -> tuple[list[Attack], list[Attack]]:
    """Divide en (visibles para el parchador, reservados). Determinista con `seed`.

    Exige al menos un visible y al menos un reservado: sin reserva no hay
    contraprueba posible.
    """
    if len(attacks) < 2:
        raise ValueError("se necesitan al menos dos ataques para reservar alguno")
    if not 0 < shown < 1:
        raise ValueError("shown debe estar entre 0 y 1")
    pool = sorted(attacks, key=lambda a: a.id)
    random.Random(seed).shuffle(pool)
    k = min(len(pool) - 1, max(1, int(len(pool) * shown)))
    return pool[:k], pool[k:]


def split_record(attacks: list[Attack], shown: float = 0.4, seed: int = 0) -> Split:
    vis, res = split(attacks, shown, seed)
    return Split(seed, shown, tuple(a.id for a in vis), tuple(a.id for a in res))


# --- Resultado de ejecutar ataques ----------------------------------------

ATTACK_OUTCOMES = ("confirmed", "blocked", "error")


@dataclass(frozen=True)
class AttackResult:
    """Resultado de un ataque. `error` no equivale a bloqueado: deja la evaluación incompleta."""

    id: str
    outcome: str  # confirmed: el predicado se cumplió; blocked: no se cumplió; error: no se pudo ejecutar
    http_status: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in ATTACK_OUTCOMES:
            raise ValueError(f"resultado desconocido: {self.outcome}")
        if self.outcome == "error":
            if not self.error:
                raise ValueError(f"{self.id}: un resultado error necesita su causa")
        else:
            # confirmed / blocked solo se afirman con una respuesta HTTP real y sin error.
            if self.error is not None:
                raise ValueError(f"{self.id}: {self.outcome} no puede llevar error")
            s = self.http_status
            if isinstance(s, bool) or not isinstance(s, int) or not 100 <= s <= 599:
                raise ValueError(f"{self.id}: {self.outcome} necesita un status HTTP válido")


@dataclass
class AttackReport:
    """Resultados de un conjunto obligatorio de ataques.

    `expected` no puede estar vacío ni repetir ids. `complete` exige exactamente
    un resultado por ataque esperado, sin extras ni errores.
    """

    expected: tuple[str, ...]
    results: list[AttackResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.expected:
            raise ValueError("un reporte de ataques necesita al menos un ataque esperado")
        if len(set(self.expected)) != len(self.expected):
            raise ValueError("ids esperados repetidos")

    @property
    def confirmed(self) -> list[str]:
        return [r.id for r in self.results if r.outcome == "confirmed"]

    @property
    def errors(self) -> list[str]:
        return [r.id for r in self.results if r.outcome == "error"]

    @property
    def complete(self) -> bool:
        """Cada ataque esperado tiene exactamente un resultado y ninguno es error."""
        ids = [r.id for r in self.results]
        return len(ids) == len(set(ids)) and set(ids) == set(self.expected) and not self.errors

    def to_dict(self) -> dict:
        return {
            "expected": list(self.expected),
            "results": [r.__dict__ for r in self.results],
            "confirmed": self.confirmed,
            "errors": self.errors,
            "complete": self.complete,
        }


def run_attacks(base_url: str, attacks: list[Attack], timeout: float = 10.0) -> AttackReport:
    """Ejecuta los ataques contra la app y devuelve un resultado por ataque (fase 2)."""
    raise NotImplementedError
