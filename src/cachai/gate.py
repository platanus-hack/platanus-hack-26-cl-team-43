"""Decisión final: aprobar o rechazar, todo o nada.

Ningún modelo participa en esta decisión. La falta de evidencia nunca cuenta
como éxito: si algo no se pudo ejecutar, la evaluación está incompleta y se
rechaza.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .integrity import Flag

# Tipos de motivo del reporte v2.
REASON_KINDS = ("test_base", "integrity", "attack_visible", "attack_hidden", "holdout_test", "incomplete")
HIDDEN_DETAIL = "el detalle queda reservado"


@dataclass(frozen=True)
class Reason:
    kind: str
    text: str  # detalle completo, para quien corre Cachai con el material reservado
    public: str  # apto para el PR: de un reservado, solo familia y ruta
    family: str = ""
    route: str = ""


@dataclass
class Evidence:
    base_tests_passed: bool
    # Detalle de cada test de la base que falla (archivo, línea, mensaje).
    base_tests_failed: list[str] = field(default_factory=list)
    integrity_flags: list[Flag] = field(default_factory=list)
    # Ataques que el parchador vio y que todavía funcionan.
    visible_attacks_succeeded: list[str] = field(default_factory=list)
    # Ataques reservados que todavía funcionan contra el código del PR.
    holdout_attacks_succeeded: list[str] = field(default_factory=list)
    # De cada uno, (familia, ruta pública), en el mismo orden. Es lo único que se muestra en el PR.
    holdout_attacks_public: list[tuple[str, str]] = field(default_factory=list)
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
    # approved: todo pasó. rejected: hay evidencia de un problema.
    # incomplete: se rechaza solo porque faltó evidencia; no se detectó nada.
    outcome: str = "approved"
    typed: list[Reason] = field(default_factory=list)


def decide(ev: Evidence) -> Verdict:
    typed: list[Reason] = []

    def add(kind: str, text: str, public: str | None = None, **kw: str) -> None:
        typed.append(Reason(kind, text, text if public is None else public, **kw))

    for step in ev.incomplete:
        add("incomplete", f"evaluación incompleta: {step}")
    unverified = len(typed)
    if ev.base_tests_failed:
        for t in ev.base_tests_failed:
            add("test_base", f"test de la base falla desde otro contenedor: {t}")
    elif not ev.base_tests_passed and not ev.incomplete:
        add("test_base", "fallan los tests de la rama base, evaluados desde otro contenedor")
    for f in ev.integrity_flags:
        if f.severity == "block":
            add("integrity", str(f))
    for a in ev.visible_attacks_succeeded:
        add("attack_visible", f"ataque visible sigue funcionando: {a}")
    public = ev.holdout_attacks_public if len(ev.holdout_attacks_public) == len(ev.holdout_attacks_succeeded) else []
    for i, a in enumerate(ev.holdout_attacks_succeeded):
        family, route = public[i] if public else ("", "")
        shown = f"{family}: {route}" if route else HIDDEN_DETAIL
        add("attack_hidden", f"ataque reservado sigue funcionando: {a}", f"ataque reservado sigue funcionando ({shown})",
            family=family, route=route)
    for t in ev.holdout_tests_failed:
        add("holdout_test", f"test funcional reservado falla: {t}", f"un test funcional reservado falla ({HIDDEN_DETAIL})")
    notes = [str(f) for f in ev.integrity_flags if f.severity != "block"]
    reasons = [r.text for r in typed]
    outcome = "approved" if not reasons else ("rejected" if len(reasons) > unverified else "incomplete")
    return Verdict(approved=not reasons, reasons=reasons, notes=notes, outcome=outcome, typed=typed)
