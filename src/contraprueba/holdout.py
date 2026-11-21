"""Ataques y tests reservados que el parchador nunca ve.

Por cada falla se generan varias variantes de ataque. Una parte se entrega al
parchador y el resto queda aquí, junto con tests funcionales, para comprobar que
el parche arregla la falla y no solo el caso que vio.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Attack:
    id: str
    family: str  # "authz" (datos de otro usuario) o "files" (acceso indebido a archivos)
    request: dict  # pedido HTTP: método, ruta, headers, cuerpo
    succeeded_if: str  # condición determinista que confirma el ataque


def split(attacks: list[Attack], shown: float = 0.4, seed: int = 0) -> tuple[list[Attack], list[Attack]]:
    """Divide en (visibles para el parchador, reservados). Determinista con `seed`."""
    pool = sorted(attacks, key=lambda a: a.id)
    random.Random(seed).shuffle(pool)
    k = max(1, int(len(pool) * shown))
    return pool[:k], pool[k:]


def run_attacks(base_url: str, attacks: list[Attack]) -> list[str]:
    """Ejecuta los ataques contra la app y devuelve los ids de los que funcionaron."""
    raise NotImplementedError
