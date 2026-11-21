"""Parchador: modelo no confiable que propone un parche.

Recibe solo los ataques visibles. Su salida pasa por la misma revisión que el
código de cualquier agente.
"""

from __future__ import annotations

from .holdout import Attack


def propose_patch(repo: str, ref: str, visible_attacks: list[Attack]) -> str:
    """Devuelve el nombre de una rama con el parche propuesto."""
    raise NotImplementedError
