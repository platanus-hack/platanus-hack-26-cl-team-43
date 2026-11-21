"""Compose de la rama base, interpretado por Cachai.

Cachai no corre `docker compose` con el archivo del repo: un compose puede
montar el socket de Docker, abrir la red del host o correr privilegiado. Lee el
archivo de la rama base y toma solo un subconjunto: imagen o build, variables
de entorno, comando y healthcheck. Todos los servicios quedan en la red interna
de la corrida, sin salida a internet; los volúmenes con nombre se descartan
(los datos son efímeros) y cualquier montaje del host es un error.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

import yaml

from .case import SERVICE_RE, safe_relpath

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

# Claves de un servicio que Cachai entiende. Las de `IGNORED` se aceptan y no tienen efecto.
USED = {"image", "build", "environment", "command", "healthcheck", "depends_on"}
IGNORED = {"ports", "expose", "container_name", "restart", "networks", "volumes", "labels", "stop_grace_period"}
TOP_LEVEL = {"services", "version", "name", "volumes", "networks"}
BUILD_KEYS = {"context", "dockerfile"}
HEALTH_KEYS = {"test", "interval", "timeout", "retries", "start_period", "disable"}


class ComposeError(ValueError):
    pass


@dataclass(frozen=True)
class Service:
    name: str
    image: str | None = None
    build_context: str | None = None  # relativo al repo
    dockerfile: str | None = None  # relativo al contexto
    environment: tuple[tuple[str, str], ...] = ()
    command: tuple[str, ...] = ()
    health_cmd: str | None = None  # para `docker run --health-cmd`
    depends_on: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class ComposePlan:
    app: Service
    services: tuple[Service, ...]  # los demás, en orden de dependencias


def _env(name: str, value: object) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    items: list[tuple[str, str]] = []
    if isinstance(value, dict):
        items = [(str(k), "" if v is None else str(v)) for k, v in value.items()]
    elif isinstance(value, list):
        for e in value:
            k, sep, v = str(e).partition("=")
            if not sep:
                raise ComposeError(f"{name}.environment: «{e}» necesita un valor (las variables del host no se heredan)")
            items.append((k, v))
    else:
        raise ComposeError(f"{name}.environment debe ser un objeto o una lista")
    for k, _ in items:
        if not ENV_NAME.match(k):
            raise ComposeError(f"{name}.environment: nombre inválido {k!r}")
    return tuple(items)


def _health(name: str, hc: object) -> str | None:
    if hc is None:
        return None
    if not isinstance(hc, dict) or set(hc) - HEALTH_KEYS:
        raise ComposeError(f"{name}.healthcheck: claves no soportadas {sorted(set(hc) - HEALTH_KEYS) if isinstance(hc, dict) else hc!r}")
    if hc.get("disable"):
        return None
    test = hc.get("test")
    if isinstance(test, str):
        return test
    if isinstance(test, list) and test and test[0] in ("CMD", "CMD-SHELL"):
        return " ".join(test[1:]) if test[0] == "CMD-SHELL" else shlex.join(str(x) for x in test[1:])
    if isinstance(test, list) and test == ["NONE"]:
        return None
    raise ComposeError(f"{name}.healthcheck.test no soportado: {test!r}")


def _volumes(name: str, vols: object) -> None:
    """Acepta volúmenes con nombre (se descartan) y rechaza todo montaje del host."""
    for v in vols or []:
        src = v.get("source", "") if isinstance(v, dict) else str(v).split(":", 1)[0]
        kind = v.get("type", "volume") if isinstance(v, dict) else ("volume" if ":" in str(v) else "anonymous")
        if "docker.sock" in str(v):
            raise ComposeError(f"{name}: montar el socket de Docker no está permitido")
        if kind == "bind" or str(src).startswith(("/", ".", "~", "$")):
            raise ComposeError(f"{name}: los montajes del host no están permitidos ({src})")


def _service(name: str, d: object) -> Service:
    if not SERVICE_RE.match(str(name)):
        raise ComposeError(f"nombre de servicio inválido: {name!r}")
    if not isinstance(d, dict):
        raise ComposeError(f"{name} debe ser un objeto")
    bad = sorted(set(d) - USED - IGNORED)
    if bad:
        raise ComposeError(f"{name}: {', '.join(bad)} no está permitido (Cachai corre cada servicio aislado, sin privilegios ni red externa)")
    _volumes(name, d.get("volumes"))
    build_context = dockerfile = None
    if "build" in d:
        b = d["build"]
        if isinstance(b, str):
            b = {"context": b}
        if not isinstance(b, dict) or set(b) - BUILD_KEYS:
            raise ComposeError(f"{name}.build: solo se admiten context y dockerfile")
        ctx = str(b.get("context", "."))
        build_context = "." if ctx in (".", "./") else safe_relpath(ctx.removeprefix("./"), f"{name}.build.context")
        dockerfile = safe_relpath(str(b["dockerfile"]).removeprefix("./"), f"{name}.build.dockerfile") if b.get("dockerfile") else None
    image = d.get("image")
    if image is not None and not isinstance(image, str):
        raise ComposeError(f"{name}.image debe ser un texto")
    if image is None and build_context is None:
        raise ComposeError(f"{name} necesita image o build")
    cmd = d.get("command")
    command = tuple(shlex.split(cmd)) if isinstance(cmd, str) else tuple(str(x) for x in cmd or ())
    deps = d.get("depends_on") or []
    deps = list(deps) if isinstance(deps, (list, dict)) else []
    return Service(name, image, build_context, dockerfile, _env(name, d.get("environment")), command,
                   _health(name, d.get("healthcheck")), tuple(str(x) for x in deps))


def parse(text: str | bytes, app_service: str) -> ComposePlan:
    try:
        d = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ComposeError(f"compose ilegible: {e}") from e
    if not isinstance(d, dict) or not isinstance(d.get("services"), dict):
        raise ComposeError("el compose necesita services")
    bad = sorted(set(d) - TOP_LEVEL)
    if bad:
        raise ComposeError(f"claves de compose no permitidas: {', '.join(bad)}")
    services = {n: _service(n, s) for n, s in d["services"].items()}
    if app_service not in services:
        raise ComposeError(f"el compose no tiene el servicio {app_service!r}")
    app = services.pop(app_service)
    if app.build_context is None:
        raise ComposeError(f"{app_service} es la app del PR: necesita build")
    for s in services.values():
        if s.build_context is not None:
            raise ComposeError(f"{s.name}: solo la app se construye; los demás servicios usan image")
    # Orden: primero los que no dependen de nadie.
    ordered: list[Service] = []
    pending = dict(services)
    while pending:
        ready = [s for s in pending.values() if all(dep not in pending for dep in s.depends_on)]
        if not ready:
            raise ComposeError("dependencias circulares en el compose")
        for s in sorted(ready, key=lambda s: s.name):
            ordered.append(pending.pop(s.name))
    return ComposePlan(app, tuple(ordered))
