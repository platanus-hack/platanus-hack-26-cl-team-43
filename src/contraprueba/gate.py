"""Decisión final: aprobar o rechazar, todo o nada.

Ningún modelo participa en esta decisión. La falta de evidencia nunca cuenta
como éxito: si algo no se pudo ejecutar, la evaluación está incompleta y se
rechaza.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .integrity import Flag


@dataclass
class Evidence:
    base_tests_passed: bool
    # Detalle de cada test de la base que falla (archivo, línea, mensaje).
    base_tests_failed: list[str] = field(default_factory=list)
    integrity_flags: list[Flag] = field(default_factory=list)
    # Ataques reservados que todavía funcionan contra el código del PR.
    holdout_attacks_succeeded: list[str] = field(default_factory=list)
    # Tests funcionales reservados que fallan.
    holdout_tests_failed: list[str] = field(default_factory=list)
    # Pasos que no terminaron (timeout, app caída, ataque que no se pudo ejecutar).
    incomplete: list[str] = field(default_factory=list)


@dataclass
class Verdict:
    approved: bool
    reasons: list[str]
    # Marcas informativas: no bloquean.
    notes: list[str] = field(default_factory=list)


def decide(ev: Evidence) -> Verdict:
    reasons = []
    for step in ev.incomplete:
        reasons.append(f"evaluación incompleta: {step}")
    if ev.base_tests_failed:
        reasons += [f"test de la base falla desde otro contenedor: {t}" for t in ev.base_tests_failed]
    elif not ev.base_tests_passed:
        reasons.append("fallan los tests de la rama base, evaluados desde otro contenedor")
    for f in ev.integrity_flags:
        if f.severity == "block":
            reasons.append(str(f))
    for a in ev.holdout_attacks_succeeded:
        reasons.append(f"ataque reservado sigue funcionando: {a}")
    for t in ev.holdout_tests_failed:
        reasons.append(f"test funcional reservado falla: {t}")
    notes = [str(f) for f in ev.integrity_flags if f.severity != "block"]
    return Verdict(approved=not reasons, reasons=reasons, notes=notes)
