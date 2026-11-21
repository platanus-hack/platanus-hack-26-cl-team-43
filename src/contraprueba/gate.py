"""Decisión final: aprobar o rechazar, todo o nada.

Ningún modelo participa en esta decisión.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .integrity import Flag


@dataclass
class Evidence:
    base_tests_passed: bool
    integrity_flags: list[Flag] = field(default_factory=list)
    # Ataques reservados que todavía funcionan contra el código del PR.
    holdout_attacks_succeeded: list[str] = field(default_factory=list)
    # Tests funcionales reservados que fallan.
    holdout_tests_failed: list[str] = field(default_factory=list)


@dataclass
class Verdict:
    approved: bool
    reasons: list[str]


def decide(ev: Evidence) -> Verdict:
    reasons = []
    if not ev.base_tests_passed:
        reasons.append("fallan los tests de la rama base, evaluados desde otro contenedor")
    for f in ev.integrity_flags:
        reasons.append(f"{f.reason} ({f.status}): {f.path}")
    for a in ev.holdout_attacks_succeeded:
        reasons.append(f"ataque reservado sigue funcionando: {a}")
    for t in ev.holdout_tests_failed:
        reasons.append(f"test funcional reservado falla: {t}")
    return Verdict(approved=not reasons, reasons=reasons)
