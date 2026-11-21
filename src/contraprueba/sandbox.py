"""App y evaluador en contenedores separados.

La app del PR corre en un contenedor; el evaluador, con los tests de la rama
base, corre en otro y le hace pedidos por HTTP. El código del PR nunca comparte
proceso ni contenedor con quien lo califica.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AppHandle:
    container_id: str
    base_url: str


def start_app(repo: str, ref: str) -> AppHandle:
    """Construye y levanta la app de `ref` en un contenedor aislado, sin salida a internet."""
    raise NotImplementedError


def run_base_tests(app: AppHandle, repo: str, base: str) -> bool:
    """Corre los tests de la rama base desde un contenedor evaluador contra `app.base_url`."""
    raise NotImplementedError


def stop(app: AppHandle) -> None:
    raise NotImplementedError
