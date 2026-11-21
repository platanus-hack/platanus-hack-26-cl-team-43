"""Plugin de pytest del evaluador: tests escritos para correr en el mismo proceso, por HTTP.

Casi todos los repos prueban con `TestClient(app)`: importan la app y le hablan
dentro del proceso. En el evaluador la app no está (corre en otro contenedor) y
importarla sería justo lo que se quiere evitar: un hook del PR podría falsear
los resultados.

Este plugin, sin tocar los tests:

- reemplaza `TestClient` (de FastAPI y de Starlette) por un cliente httpx
  contra `BASE_URL`, que ignora la app que recibe;
- cuando un test importa un módulo que en el evaluador no existe (la app, sus
  dependencias, FastAPI mismo), le entrega un módulo sustituto. El sustituto es
  perezoso: llamarlo, entrar a un `with`, asignarle `dependency_overrides`
  devuelve otro sustituto y deja anotado que el test usó código de la app.
  Leer un valor suyo (compararlo, convertirlo a texto, iterarlo, usarlo en un
  `if`) lanza `NotFromOutside`: ningún test aprueba gracias a un sustituto;
- un test que lanza `NotFromOutside`, o que falla después de usar código de la
  app (una fixture que llenó la base, un override), no cuenta como fallo ni
  como aprobado: queda como «no evaluado desde afuera», con el motivo, y el
  reporte lo lista. Si aprueba, aprobó contra la app real por HTTP;
- lo que solo se le pasa a `TestClient` (`TestClient(create_app())`) no cuenta
  como uso de la app;
- `fastapi.status` y `starlette.status` se entregan de verdad (son solo
  números), aunque FastAPI no esté instalado en el evaluador.

Solo se sustituyen importaciones hechas desde los archivos de tests (también
los `conftest.py`); las de pytest, httpx y la biblioteca estándar siguen su
camino normal.
"""

from __future__ import annotations

import http
import importlib
import importlib.abc
import importlib.machinery
import itertools
import json
import os
import re
import sys
import types
from pathlib import Path

import httpx
import pytest

from .holdout import CLIENT_HEADERS_ENV

MARK = "cachai:no-externo"
CLIENT_MODULES = ("fastapi.testclient", "starlette.testclient")
STATUS_MODULES = ("fastapi.status", "starlette.status")


class NotFromOutside(BaseException):
    """El test usa el código de la app, no solo su API HTTP.

    Hereda de `BaseException` para que un `except Exception` o un
    `pytest.raises(Exception)` del test no lo trague y lo haga aprobar.
    """


# Usos del código de la app, por id. Cada test y cada fixture en curso anota
# los que ocurren mientras corre; pasarle un sustituto a `TestClient` borra los
# que lo produjeron.
_ids = itertools.count(1)
_active: list[dict[int, str]] = []
_fixture_uses: dict[str, dict[int, str]] = {}


def _used(what: str) -> int:
    uid = next(_ids)
    for scope in _active:
        scope[uid] = what
    return uid


def _forgive(uids: frozenset[int]) -> None:
    for scope in [*_active, *_fixture_uses.values()]:
        for uid in uids:
            scope.pop(uid, None)


def _reason(what: str) -> str:
    return f"usa {what}, que no está en el evaluador"


class Stub:
    """Lo que entrega un módulo sustituto: acepta todo menos que se lea su valor."""

    __slots__ = ("_name", "_uids")

    def __init__(self, name: str, uids: frozenset[int] = frozenset()) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_uids", uids)

    def _fail(self, what: str = ""):
        raise NotFromOutside(_reason(f"el valor de {self._name}{what}"))

    def _child(self, name: str, action: str | None = None) -> Stub:
        uids = self._uids
        if action is not None:
            uids = uids | {_used(f"{self._name}{action}")}
        return Stub(name, uids)

    def __getattr__(self, attr: str):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        return self._child(f"{self._name}.{attr}")

    def __call__(self, *a, **k):
        return self._child(f"{self._name}()", "()")

    def __getitem__(self, key):
        return self._child(f"{self._name}[...]")

    def __setattr__(self, attr, value):
        _used(f"{self._name}.{attr} (asignación)")

    def __delattr__(self, attr):
        _used(f"{self._name}.{attr} (borrado)")

    def __setitem__(self, key, value):
        _used(f"{self._name}[...] (asignación)")

    def __delitem__(self, key):
        _used(f"{self._name}[...] (borrado)")

    def __enter__(self):
        return self._child(self._name, " (with)")

    def __exit__(self, *exc):
        return False

    async def __aenter__(self):
        return self._child(self._name, " (async with)")

    async def __aexit__(self, *exc):
        return False

    def __await__(self):
        return self._child(self._name, " (await)")
        yield  # noqa: unreachable; hace de esto un generador

    __hash__ = object.__hash__

    def __repr__(self) -> str:
        return f"<sustituto de {self._name}>"


def _observe(self, *a, **k):
    self._fail()


for _op in ("bool", "eq", "ne", "lt", "le", "gt", "ge", "iter", "aiter", "len", "contains", "str", "format",
            "int", "float", "index", "bytes", "complex", "neg", "pos", "abs", "invert",
            *(f"{p}{o}" for o in ("add", "sub", "mul", "truediv", "floordiv", "mod", "pow", "and", "or", "xor",
                                  "matmul", "lshift", "rshift") for p in ("", "r"))):
    setattr(Stub, f"__{_op}__", _observe)


class HTTPTestClient(httpx.Client):
    """`TestClient(app)` que habla por HTTP con la app del otro contenedor."""

    __test__ = False

    def __init__(self, app=None, base_url: str = "http://testserver", raise_server_exceptions: bool = True,
                 root_path: str = "", backend: str = "asyncio", backend_options=None, cookies=None,
                 headers=None, follow_redirects: bool = True, **_ignored) -> None:
        if isinstance(app, Stub):
            _forgive(app._uids)  # construir la app para `TestClient` no es usarla
        super().__init__(base_url=os.environ["BASE_URL"], cookies=cookies, headers=headers,
                         follow_redirects=follow_redirects, timeout=30)

    def send(self, request, **kwargs):
        global _requests
        _requests += 1
        return super().send(request, **kwargs)


_requests = 0  # pedidos HTTP a la app durante el test en curso


def _client_module(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    m.TestClient = HTTPTestClient
    return m


def _status_module(name: str) -> types.ModuleType:
    """`status.HTTP_201_CREATED` y compañía: los números salen del nombre."""
    m = types.ModuleType(name)
    for s in http.HTTPStatus:
        setattr(m, f"HTTP_{s.value}_{s.name}", s.value)

    def by_name(attr: str):
        found = re.fullmatch(r"(?:HTTP|WS)_(\d{3,4})_\w+", attr)
        if not found:
            raise AttributeError(attr)
        return int(found.group(1))

    m.__getattr__ = by_name
    return m


def _stub_module(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__path__ = []  # paquete: admite submódulos

    def attr(a: str):
        if a.startswith("__") and a.endswith("__"):
            raise AttributeError(a)
        if f"{name}.{a}" in STATUS_MODULES + CLIENT_MODULES:
            return importlib.import_module(f"{name}.{a}")
        return Stub(f"{name}.{a}")

    m.__getattr__ = attr
    m.__cachai_stub__ = True
    return m


class _Loader(importlib.abc.Loader):
    def __init__(self, factory) -> None:
        self.factory = factory

    def create_module(self, spec):
        return self.factory(spec.name)

    def exec_module(self, module) -> None:
        pass


class StubFinder(importlib.abc.MetaPathFinder):
    """Va al final de `sys.meta_path`: solo actúa si nadie más encontró el módulo."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = [str(r.resolve()) for r in roots]
        self.stubbed: set[str] = set()

    def _from_tests(self) -> bool:
        f = sys._getframe(2)
        while f is not None:
            name = f.f_code.co_filename
            if not name.startswith("<") and "importlib" not in name:
                return any(name.startswith(r) for r in self.roots)
            f = f.f_back
        return False

    def find_spec(self, fullname, path=None, target=None):
        if fullname in CLIENT_MODULES:
            return importlib.machinery.ModuleSpec(fullname, _Loader(_client_module))
        if not self._from_tests() and fullname.split(".")[0] not in self.stubbed:
            return None
        if fullname in STATUS_MODULES:
            return importlib.machinery.ModuleSpec(fullname, _Loader(_status_module))
        self.stubbed.add(fullname.split(".")[0])
        return importlib.machinery.ModuleSpec(fullname, _Loader(_stub_module), is_package=True)


_finder: StubFinder | None = None


def use_default_headers(headers: dict[str, str]) -> None:
    """Todo cliente httpx que creen los tests (también el del conftest del repo)
    manda estas cabeceras salvo que el test fije las suyas: el mismo perfil de
    navegador que los ataques, así la app no distingue tests de ataques (#18)."""
    defaults = httpx.Headers({"User-Agent": f"python-httpx/{httpx.__version__}", "Accept": "*/*"})
    for cls in (httpx.Client, httpx.AsyncClient):
        if getattr(cls.__init__, "_cachai", False):
            continue
        original = cls.__init__

        def __init__(self, *args, _original=original, **kwargs):
            _original(self, *args, **kwargs)
            for k, v in headers.items():
                # Solo donde el cliente tiene el valor por defecto de httpx: lo que fijó el test se respeta.
                if self.headers.get(k) in (None, defaults.get(k)):
                    self.headers[k] = v

        __init__._cachai = True
        cls.__init__ = __init__


def _install(rootpath) -> None:
    global _finder
    if _finder is not None or not os.environ.get("BASE_URL"):
        return
    if os.environ.get(CLIENT_HEADERS_ENV):
        use_default_headers(json.loads(os.environ[CLIENT_HEADERS_ENV]))
    for name in CLIENT_MODULES:
        sys.modules[name] = _client_module(name)
    _finder = StubFinder([Path(str(rootpath))])
    sys.meta_path.append(_finder)


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args) -> None:
    # Antes de cargar los conftest.py de la raíz, que suelen importar la app.
    _install(early_config.rootpath)


def pytest_configure(config) -> None:
    _install(config.rootpath)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _requests
    _active[:] = [{}]
    _requests = 0
    yield
    _active.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    uses: dict[int, str] = {}
    _active.append(uses)
    try:
        yield
    finally:
        _active.remove(uses)
        if uses:
            _fixture_uses.setdefault(fixturedef.argname, {}).update(uses)


def _app_use(item) -> str | None:
    """Si el test o una de sus fixtures usó código de la app, cuál fue el primero."""
    scopes = _active[:1] + [_fixture_uses.get(name, {}) for name in getattr(item, "fixturenames", ())]
    firsts = [min(s.items()) for s in scopes if s]
    return min(firsts)[1] if firsts else None


def _outside(excinfo) -> str | None:
    if excinfo is None:
        return None
    if excinfo.errisinstance(NotFromOutside):
        return str(excinfo.value)
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if _finder is None:
        return
    why = None
    if report.outcome == "failed":
        why = _outside(call.excinfo)
        if why is None and (used := _app_use(item)):
            why = f"falla después de usar {used}, que no está en el evaluador"
    elif report.outcome == "passed" and report.when == "call" and not _requests and (used := _app_use(item)):
        # Aprobó sin hablarle a la app: lo único que probó fue el sustituto.
        why = f"no le habla a la app por HTTP y usa {used}, que no está en el evaluador"
    if why:
        report.outcome = "skipped"
        report.longrepr = (str(item.path), item.location[1] or 0, f"{MARK}: {why}")


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    outcome = yield
    report = outcome.get_result()
    if report.outcome == "failed" and MARK not in str(report.longrepr):
        text = str(report.longrepr)
        if "NotFromOutside" in text:
            lines = [ln for ln in text.splitlines() if "NotFromOutside: " in ln]
            line = lines[-1] if lines else "usa el código de la app al importar el módulo"
            report.outcome = "skipped"
            report.longrepr = (str(collector.path), 0, f"{MARK}: {line.split('NotFromOutside:', 1)[-1].strip()}")
