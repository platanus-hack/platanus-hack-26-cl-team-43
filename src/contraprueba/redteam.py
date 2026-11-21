"""Equipo rojo: genera variantes de ataque por falla.

Solo contra apps propias dentro del sandbox. Cada ataque se confirma con una
condición determinista (ver `holdout.Attack.succeeded_if`), nunca con un modelo.
"""

from __future__ import annotations

from .holdout import Attack


def generate_attacks(repo: str, ref: str, family: str, n: int = 10) -> list[Attack]:
    raise NotImplementedError
