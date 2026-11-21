"""Ataques y tests reservados que el parchador nunca ve.

Por cada falla se generan varias variantes de ataque. Una parte se entrega al
parchador y el resto queda aquí, junto con tests funcionales, para comprobar que
el parche arregla la falla y no solo el caso que vio.

Un ataque se confirma con un predicado estructurado sobre la respuesta HTTP,
nunca ejecutando código ni preguntándole a un modelo.

Semántica del pedido: `path` es relativo al origen de la app (empieza con una
sola `/`, sin esquema ni host) y se envía tal cual. Las redirecciones se siguen
solo dentro del mismo origen, hasta `MAX_HOPS` saltos, y el predicado se evalúa
sobre cada respuesta de la cadena: si los datos aparecen en cualquiera, el
ataque funcionó. Si la cadena no termina (más saltos, o sale del origen) y no
se vio la fuga, el ataque queda sin veredicto, no bloqueado.

Los ataques usan el mismo cliente HTTP que los tests (httpx con su
configuración por defecto), así que sus pedidos tienen el mismo formato. Hay un
cliente por identidad: ningún usuario hereda la sesión de otro. Cada ataque
puede llevar un control: el mismo pedido hecho por el dueño
del recurso, que debe seguir funcionando. Si el control falla, la app está
negando el servicio a ese cliente y el ataque no cuenta como bloqueado.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

FAMILIES = ("authz", "files")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
ATTACK_KEYS = {"id", "family", "request", "succeeded_if"}
ATTACK_OPTIONAL_KEYS = {"group", "control"}
CONTROL_KEYS = {"request", "expect"}
MAX_BODY = 20_000_000  # una respuesta más grande no se puede revisar: queda como error
MAX_HOPS = 5
REDIRECTS = (301, 302, 303, 307, 308)
REQUEST_KEYS = {"method", "path", "headers", "json"}
PREDICATE_KEYS = {"status_in", "body_contains", "body_contains_any", "body_not_contains"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SAFE_SEGMENT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")  # tramo de ruta fijo, que no es un id ni un archivo
MIN_SECRET = 4  # textos más cortos aparecerían por casualidad en cualquier mensaje


def _leaves(v: object) -> list:
    if isinstance(v, dict):
        return [x for k, val in v.items() for x in [k, *_leaves(val)]]
    if isinstance(v, list):
        return [x for val in v for x in _leaves(val)]
    return [v]


def _str_list(d: dict, key: str) -> tuple[str, ...]:
    v = d.get(key, [])
    if not isinstance(v, list) or not all(isinstance(s, str) and s for s in v):
        raise ValueError(f"{key} debe ser una lista de textos no vacíos")
    return tuple(v)


@dataclass(frozen=True)
class Predicate:
    """Condición determinista que confirma que un ataque funcionó.

    Se cumplen todas las partes presentes: el status está en `status_in`, el
    cuerpo contiene cada texto de `body_contains`, al menos uno de
    `body_contains_any` y ninguno de `body_not_contains`.

    Para detectar una fuga de datos conviene no fijar el status: los datos de
    otro usuario en la respuesta son una fuga con cualquier código.
    """

    status_in: tuple[int, ...] = ()
    body_contains: tuple[str, ...] = ()
    body_not_contains: tuple[str, ...] = ()
    body_contains_any: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not (self.status_in or self.body_contains or self.body_contains_any):
            raise ValueError("el predicado necesita status_in, body_contains o body_contains_any")
        for s in self.status_in:
            if isinstance(s, bool) or not isinstance(s, int) or not 100 <= s <= 599:
                raise ValueError(f"status inválido: {s!r}")

    def matches(self, status: int, body: str) -> bool:
        if self.status_in and status not in self.status_in:
            return False
        if any(s not in body for s in self.body_contains):
            return False
        if self.body_contains_any and not any(s in body for s in self.body_contains_any):
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
            body_contains_any=_str_list(d, "body_contains_any"),
        )

    def to_dict(self) -> dict:
        d: dict = {}
        if self.status_in:
            d["status_in"] = list(self.status_in)
        if self.body_contains:
            d["body_contains"] = list(self.body_contains)
        if self.body_contains_any:
            d["body_contains_any"] = list(self.body_contains_any)
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
class Control:
    """Pedido del dueño del recurso que debe seguir funcionando con el mismo cliente."""

    request: dict
    expect: Predicate

    def __post_init__(self) -> None:
        validate_request(self.request)
        if not isinstance(self.expect, Predicate):
            raise ValueError("expect debe ser un Predicate")

    @classmethod
    def from_dict(cls, d: dict) -> "Control":
        if not isinstance(d, dict) or set(d) != CONTROL_KEYS:
            raise ValueError("control inválido")
        return cls(dict(d["request"]) if isinstance(d["request"], dict) else d["request"], Predicate.from_dict(d["expect"]))

    def to_dict(self) -> dict:
        return {"request": self.request, "expect": self.expect.to_dict()}


@dataclass(frozen=True)
class Attack:
    id: str
    family: str  # "authz" (datos de otro usuario) o "files" (acceso indebido a archivos)
    request: dict  # pedido HTTP: method, path, headers, json (opcional)
    succeeded_if: Predicate
    # Variante a la que pertenece (una ruta, una forma de escribir la ruta del archivo).
    # La selección visible/reservada se hace por grupos completos.
    group: str = ""
    control: Control | None = None

    def __post_init__(self) -> None:
        if self.control is not None and not isinstance(self.control, Control):
            raise ValueError("control debe ser un Control")
        if not isinstance(self.id, str) or not ID_RE.match(self.id):
            raise ValueError(f"id de ataque inválido: {self.id!r}")
        if not isinstance(self.group, str) or (self.group and not ID_RE.match(self.group)):
            raise ValueError(f"grupo de ataque inválido: {self.group!r}")
        if self.family not in FAMILIES:
            raise ValueError(f"familia desconocida: {self.family}")
        validate_request(self.request)
        if not isinstance(self.succeeded_if, Predicate):
            raise ValueError("succeeded_if debe ser un Predicate")

    @classmethod
    def from_dict(cls, d: dict) -> "Attack":
        if not isinstance(d, dict):
            raise ValueError("el ataque debe ser un objeto")
        unknown = set(d) - ATTACK_KEYS - ATTACK_OPTIONAL_KEYS
        missing = ATTACK_KEYS - set(d)
        if unknown or missing:
            raise ValueError(f"ataque con claves inválidas: sobran {sorted(unknown)}, faltan {sorted(missing)}")
        return cls(
            id=d["id"],
            family=d["family"],
            request=dict(d["request"]) if isinstance(d["request"], dict) else d["request"],
            succeeded_if=Predicate.from_dict(d["succeeded_if"]),
            group=d.get("group", ""),
            control=Control.from_dict(d["control"]) if d.get("control") is not None else None,
        )

    @property
    def group_key(self) -> tuple[str, str]:
        return (self.family, self.group or self.id)

    def describe(self) -> str:
        return f"{self.id} ({self.request['method']} {self.request['path']})"

    def public_route(self) -> str:
        """Método y ruta sin consulta, con los tramos variables ocultos: lo único
        de un ataque reservado que se puede mostrar en un PR."""
        path = self.request["path"].split("?", 1)[0].split("#", 1)[0]
        parts = [p if SAFE_SEGMENT.match(p) else "{…}" for p in path.split("/")[1:]]
        return f"{self.request['method']} /" + "/".join(parts)

    def secrets(self) -> set[str]:
        """Textos de un ataque reservado que no pueden salir en un reporte público:
        id, pedidos (ruta, headers, cuerpo) y las marcas de los predicados."""
        out = {self.id, self.group}
        requests = [self.request] + ([self.control.request] if self.control else [])
        predicates = [self.succeeded_if] + ([self.control.expect] if self.control else [])
        for r in requests:
            out.add(r["path"])
            out.update(p for p in r["path"].replace("?", "&").split("&")[1:])
            out.update(str(v) for v in (r.get("headers") or {}).values())
            if "json" in r:
                out.add(json.dumps(r["json"], ensure_ascii=False))
                out.update(str(v) for v in _leaves(r["json"]))
        for pred in predicates:
            for v in pred.to_dict().values():
                out.update(str(x) for x in v if isinstance(x, str))
        return {x for x in out if len(x) >= MIN_SECRET}

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "family": self.family,
            "request": self.request,
            "succeeded_if": self.succeeded_if.to_dict(),
        }
        if self.group:
            d["group"] = self.group
        if self.control:
            d["control"] = self.control.to_dict()
        return d


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

    Se reparten grupos completos, familia por familia: el parchador ve algunas
    variantes de la falla (algunas rutas, algunas formas del ataque) y las demás
    quedan reservadas enteras. Así la reserva mide si el parche arregla la
    falla y no solo las variantes que vio. Una familia con un solo grupo queda
    reservada. Exige al menos un ataque visible y al menos uno reservado.
    """
    if not 0 < shown < 1:
        raise ValueError("shown debe estar entre 0 y 1")
    rng = random.Random(seed)
    visible_groups: set[tuple[str, str]] = set()
    for family in sorted({a.family for a in attacks}):
        groups = sorted({a.group_key for a in attacks if a.family == family})
        rng.shuffle(groups)
        if len(groups) >= 2:
            k = min(len(groups) - 1, max(1, int(len(groups) * shown)))
            visible_groups.update(groups[:k])
    ordered = sorted(attacks, key=lambda a: a.id)
    visible = [a for a in ordered if a.group_key in visible_groups]
    reserved = [a for a in ordered if a.group_key not in visible_groups]
    if not visible or not reserved:
        raise ValueError("se necesitan al menos dos grupos de ataques en alguna familia para reservar alguno")
    return visible, reserved


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


def searchable(body: str) -> str:
    """Texto donde se busca la marca: el cuerpo tal cual y, si es JSON, también
    su forma decodificada (para que un escape `\\u0046` no esconda el dato)."""
    try:
        return body + "\n" + json.dumps(json.loads(body), ensure_ascii=False)
    except (ValueError, RecursionError):
        return body


class Unverifiable(Exception):
    """El pedido se hizo, pero no hay base para decir si los datos quedaron protegidos."""


def exchange(client, base_url: str, request: dict, deadline: float, timeout: float = 10.0) -> tuple[list[tuple[int, str]], str | None]:
    """Hace un pedido y devuelve ([(status, cuerpo)] por cada respuesta, pendiente).

    La ruta se envía tal cual. Sigue redirecciones del mismo origen hasta
    `MAX_HOPS`. `pendiente` no es None cuando quedó un destino sin inspeccionar
    (se acabaron los saltos o la redirección sale del origen): en ese caso la
    cadena no terminó y no se puede afirmar que no haya fuga. Lanza una
    excepción si se agota el plazo, si falla la conexión o si una respuesta no
    se puede leer.
    """
    import httpx

    origin = httpx.URL(base_url)
    headers = dict(request.get("headers", {}))
    # La URL final se arma antes de construir el pedido: el cliente elige las
    # cookies según la ruta, y una sesión limitada a un subdirectorio debe viajar.
    url = origin.copy_with(raw_path=request["path"].encode())
    req = client.build_request(request["method"], url, headers=headers, json=request.get("json"))
    hops: list[tuple[int, str]] = []
    for hop in range(MAX_HOPS + 1):
        try:
            status, location, body = _fetch(client, req, deadline, timeout)
        except httpx.TransportError as e:
            if isinstance(e, httpx.TimeoutException):
                raise
            # La app pudo cerrar la conexión reutilizada: un reintento con conexión nueva.
            status, location, body = _fetch(client, req, deadline, timeout)
        hops.append((status, body))
        if status not in REDIRECTS or not location:
            return hops, None
        target = req.url.join(location)
        if (target.scheme, target.host, target.port) != (origin.scheme, origin.host, origin.port):
            return hops, "la respuesta redirige a otro origen, que no se inspecciona"
        if hop == MAX_HOPS:
            break
        method = request["method"] if status in (307, 308) else "GET"
        req = client.build_request(method, target, headers=headers)
    return hops, f"más de {MAX_HOPS} redirecciones seguidas"


BOMS = ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))


def decode_body(data: bytes, declared: str | None) -> str:
    """Texto del cuerpo según lo que declara la respuesta HTTP.

    Usa el charset del Content-Type; si no hay, la marca de orden de bytes; si
    tampoco, UTF-8. Un charset desconocido o bytes que no corresponden son
    `Unverifiable`: no se adivina, porque una mala lectura escondería una fuga.
    """
    encoding = declared
    if not encoding:
        encoding = next((name for bom, name in BOMS if data.startswith(bom)), "utf-8")
    try:
        text = data.decode(encoding)
    except (LookupError, UnicodeDecodeError) as e:
        raise Unverifiable(f"no se pudo leer la respuesta como {encoding}") from e
    if "\x00" in text:
        # Un texto no trae bytes nulos: es binario u otra codificación sin declarar.
        raise Unverifiable("la respuesta no es texto legible (trae bytes nulos)")
    return text


def _fetch(client, req, deadline: float, timeout: float) -> tuple[int, str | None, str]:
    """Envía un pedido y lee su cuerpo dentro del plazo. Devuelve (status, Location, cuerpo)."""
    import httpx

    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("presupuesto agotado")
    req.extensions["timeout"] = httpx.Timeout(min(timeout, left)).as_dict()
    resp = client.send(req, stream=True)
    try:
        chunks, size = [], 0
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > MAX_BODY:
                raise Unverifiable("respuesta demasiado grande para revisar")
            if time.monotonic() > deadline:
                raise TimeoutError("presupuesto agotado leyendo la respuesta")
            chunks.append(chunk)
    finally:
        resp.close()
    return resp.status_code, resp.headers.get("location"), decode_body(b"".join(chunks), resp.charset_encoding)


def check(predicate: Predicate, hops: list[tuple[int, str]], pending: str | None) -> tuple[bool, int]:
    """¿Alguna respuesta de la cadena cumple el predicado? Devuelve (sí o no, status a informar).

    Una marca encontrada en cualquier salto vale aunque la cadena no haya
    terminado. Si no se encontró y quedó un destino sin inspeccionar, no hay
    veredicto: lanza `Unverifiable`.
    """
    for status, body in hops:
        if predicate.matches(status, searchable(body)):
            return True, status
    if pending:
        raise Unverifiable(pending)
    return False, hops[-1][0]


class Clients:
    """Un cliente por identidad, igual al que usan los tests (httpx por defecto).

    Cada usuario conserva su propia sesión (cookies, conexión) entre pedidos, y
    nunca la comparte con otro: la respuesta que recibe el dueño en un control
    no puede darle acceso al otro usuario en el ataque.
    """

    def __init__(self) -> None:
        self._clients: dict[str, object] = {}

    def for_request(self, request: dict):
        import httpx

        key = json.dumps(sorted((k.lower(), v) for k, v in request.get("headers", {}).items()))
        if key not in self._clients:
            self._clients[key] = httpx.Client(timeout=10)
        return self._clients[key]

    def __enter__(self) -> "Clients":
        return self

    def __exit__(self, *exc) -> None:
        for c in self._clients.values():
            c.close()


def run_attacks(base_url: str, attacks: list[Attack], deadline: float | None = None, timeout: float = 10.0) -> AttackReport:
    """Ejecuta los ataques contra la app y devuelve un resultado por ataque.

    Antes de cada ataque corre su control. Queda como `error`, no como
    bloqueado, todo ataque sin veredicto: no se pudo ejecutar (conexión, plazo),
    su respuesta no se pudo leer, la cadena de redirecciones no terminó o su
    control falló. `deadline` es un instante de `time.monotonic()`.
    """
    deadline = time.monotonic() + timeout * max(len(attacks), 1) * 2 if deadline is None else deadline
    report = AttackReport(tuple(a.id for a in attacks))
    with Clients() as clients:
        for a in attacks:
            try:
                if a.control is not None:
                    req = a.control.request
                    try:
                        ok, status = check(a.control.expect, *exchange(clients.for_request(req), base_url, req, deadline, timeout))
                    except Unverifiable as e:
                        raise Unverifiable(f"control: {e}") from e
                    if not ok:
                        report.results.append(AttackResult(a.id, "error", error=f"control: el dueño ya no obtiene su recurso (status {status})"))
                        continue
                confirmed, status = check(a.succeeded_if, *exchange(clients.for_request(a.request), base_url, a.request, deadline, timeout))
            except Exception as e:  # conexión, plazo, respuesta ilegible, cadena sin terminar: no hay veredicto
                report.results.append(AttackResult(a.id, "error", error=(str(e) if isinstance(e, Unverifiable) else f"{type(e).__name__}: {e}")[:200]))
                continue
            report.results.append(AttackResult(a.id, "confirmed" if confirmed else "blocked", status))
    return report
