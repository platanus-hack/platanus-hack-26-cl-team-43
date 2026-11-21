"""Plugin de pytest del evaluador: tests escritos para correr en el mismo proceso, por HTTP.

Casi todos los repos prueban con `TestClient(app)`: importan la app y le hablan
dentro del proceso. En el evaluador la app no está (corre en otro contenedor) y
importarla sería justo lo que se quiere evitar: un hook del PR podría falsear
los resultados.

Este plugin, sin tocar los tests:

- reemplaza `TestClient` (de FastAPI y de Starlette) por un cliente httpx
  contra `BASE_URL`, que ignora la app que recibe;
- cuando un test importa un módulo que en el evaluador no existe (la app, sus
  dependencias), le entrega un módulo sustituto. Pasar ese sustituto a
  `TestClient` funciona; usarlo para otra cosa (llamar una función de la app,
  tocar `dependency_overrides`, abrir la base de datos) lanza `NotFromOutside`;
- un test que choca con un sustituto no cuenta como fallo ni como aprobado:
  queda como «no evaluado desde afuera», con el motivo, y el reporte lo lista.

Solo se sustituyen importaciones hechas desde los archivos de tests; las de
pytest, httpx y la biblioteca estándar siguen su camino normal.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
import sys
import types
from pathlib import Path

import httpx
import pytest

MARK = "cachai:no-externo"
CLIENT_MODULES = ("fastapi.testclient", "starlette.testclient")


class NotFromOutside(Exception):
    """El test usa el código de la app, no solo su API HTTP."""


class Stub:
    """Lo que entrega un módulo sustituto. Solo sirve para pasárselo a `TestClient`."""

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)

    def _fail(self, what: str):
        raise NotFromOutside(f"usa {self._name}{what}, que no está en el evaluador")

    def __getattr__(self, attr: str):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        self._fail(f".{attr}")

    def __setattr__(self, attr, value):
        self._fail(f".{attr} (asignación)")

    def __call__(self, *a, **k):
        self._fail("()")

    def __getitem__(self, key):
        self._fail(f"[{key!r}]")

    def __iter__(self):
        self._fail(" (iteración)")

    def __bool__(self):
        return True

    def __repr__(self) -> str:
        return f"<sustituto de {self._name}>"


class HTTPTestClient(httpx.Client):
    """`TestClient(app)` que habla por HTTP con la app del otro contenedor."""

    def __init__(self, app=None, base_url: str = "http://testserver", raise_server_exceptions: bool = True,
                 root_path: str = "", backend: str = "asyncio", backend_options=None, cookies=None,
                 headers=None, follow_redirects: bool = True, **_ignored) -> None:
        super().__init__(base_url=os.environ["BASE_URL"], cookies=cookies, headers=headers,
                         follow_redirects=follow_redirects, timeout=30)


def _client_module(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    m.TestClient = HTTPTestClient
    return m


def _stub_module(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__path__ = []  # paquete: admite submódulos
    m.__getattr__ = lambda attr: Stub(f"{name}.{attr}")
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
        self.stubbed.add(fullname.split(".")[0])
        return importlib.machinery.ModuleSpec(fullname, _Loader(_stub_module), is_package=True)


_finder: StubFinder | None = None


def pytest_configure(config) -> None:
    global _finder
    if not os.environ.get("BASE_URL"):
        return
    for name in CLIENT_MODULES:
        sys.modules[name] = _client_module(name)
    _finder = StubFinder([Path(str(config.rootpath))])
    sys.meta_path.append(_finder)


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
    why = _outside(call.excinfo)
    if why and report.outcome == "failed":
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
