"""Texto público del veredicto: la línea del check y el comentario del PR.

Se arma solo con el reporte v2 ya público (`CheckResult.report`): de un ataque
reservado, familia y ruta; de un test reservado, cuántos fallan. `pipeline`
pasa después todo el reporte por `_redact`, así que si un texto reservado se
colara por un motivo, igual sale como `[reservado]`.

El detalle de cada motivo viene del código del PR (mensajes de assert, rutas de
archivos), así que va dentro de bloques de código: así no puede inyectar HTML,
menciones ni un marcador falso en el comentario.
"""

from __future__ import annotations

import re

# Marcador oculto con el que la Action encuentra su comentario para editarlo en vez de sumar uno por push.
MARKER = "<!-- cachai-report -->"

OUTCOME_TITLE = {"approved": "APROBADO", "rejected": "RECHAZADO", "incomplete": "SIN VEREDICTO"}

MODE_DESC = {
    "ci": "pytest con los tests y la configuración del PR, en el mismo contenedor que su código (línea base, como un CI normal).",
    "restore": "tests, configuración y hooks de la rama base, con pytest en el mismo contenedor que el código del PR (línea base).",
    "visible": "la app del PR en un contenedor y el evaluador en otro, con los tests de la rama base y los ataques visibles.",
    "full": "la app del PR en un contenedor y el evaluador en otro, con los tests de la rama base, los ataques visibles y "
            "reservados, los tests funcionales reservados y la revisión de los cambios que tocan la evaluación.",
}

# Orden de las secciones de motivos y su título.
KIND_TITLE = {
    "test_base": "Tests de la rama base que fallan",
    "integrity": "Cambios que tocan la evaluación",
    "attack_visible": "Ataques visibles que siguen funcionando",
    "attack_hidden": "Ataques reservados que siguen funcionando",
    "attack_introduced": "Vulnerabilidades que introduce el PR",
    "holdout_test": "Tests funcionales reservados",
    "incomplete": "Evaluación incompleta",
}
# Prefijo que `gate.decide` pone a cada motivo; en el comentario ya lo dice el título de la sección.
KIND_PREFIX = {
    "test_base": "test de la base falla desde otro contenedor: ",
    "attack_visible": "ataque visible sigue funcionando: ",
    "attack_introduced": "vulnerabilidad introducida por el PR: ",
    "incomplete": "evaluación incompleta: ",
}
KIND_SHORT = {
    "test_base": ("test de la base", "tests de la base"),
    "integrity": ("cambio en la evaluación", "cambios en la evaluación"),
    "attack_visible": ("ataque visible", "ataques visibles"),
    "attack_hidden": ("ataque reservado", "ataques reservados"),
    "attack_introduced": ("vulnerabilidad introducida", "vulnerabilidades introducidas"),
    "holdout_test": ("test reservado", "tests reservados"),
    "incomplete": ("paso incompleto", "pasos incompletos"),
}
LOCATION = re.compile(r"^(\S+:\d+)\s+(.*)$", re.S)
VISIBLE_ATTACK = re.compile(r"^(\S+) \(([A-Z]+ \S.*)\)$", re.S)  # `Attack.describe`: id (MÉTODO ruta)
MAX_PER_KIND = 15  # motivos por sección; el resto va contado
MAX_SUMMARY = 200  # largo del título de un check run


def code(text: str) -> str:
    """Texto como código en línea, a prueba de comillas invertidas, saltos de línea y comentarios HTML."""
    # Sin «<!--»: un detalle del PR no puede llevar una copia del marcador del comentario.
    text = " ".join(str(text).split()).replace("<!--", "<!- -")
    if not text:
        return ""
    fence = "`" * (max((len(m) for m in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def repro_command(report: dict) -> str:
    """Comando para correr en local la parte visible de la revisión (sin `--mode`: modo visible)."""
    return f"cachai check --repo . --base {report['base']} --pr {report['pr']}"


def _counts(reasons: list[dict]) -> str:
    out = []
    for kind in KIND_TITLE:
        n = sum(r["kind"] == kind for r in reasons)
        if n:
            one, many = KIND_SHORT[kind]
            out.append(f"{n} {one if n == 1 else many}")
    return ", ".join(out)


def summary(report: dict) -> str:
    """Una línea para la lista de checks del PR."""
    title = OUTCOME_TITLE[report["outcome"]]
    if report["approved"]:
        line = f"Cachai: {title}. " + "; ".join(report["checked"])
    else:
        n = len(report["reasons"])
        line = f"Cachai: {title}, {n} motivo{'s' if n != 1 else ''}"
        counts = _counts(report["reasons"])
        line += f" ({counts})." if counts else "."
    return line if len(line) <= MAX_SUMMARY else line[: MAX_SUMMARY - 1] + "…"


def reason_line(reason: dict) -> str:
    """Un motivo con su lugar (archivo y línea, o ruta) separado del detalle."""
    kind, text = reason["kind"], reason["text"]
    if kind == "attack_hidden":
        if reason.get("route"):
            return f"- {code(reason.get('family') or '?')} {code(reason['route'])}"
        return "- el detalle queda reservado"
    if kind == "holdout_test":
        return f"- {text}"  # ya es público: no nombra el test
    prefix = KIND_PREFIX.get(kind, "")
    if prefix and text.startswith(prefix):
        text = text[len(prefix):]
    if kind == "attack_visible" and (m := VISIBLE_ATTACK.match(text)):
        attack_id, route = m.groups()
        return f"- {code(route)} {code(attack_id)}"
    if kind == "integrity" and "): " in text:
        what, where = text.split("): ", 1)
        loc, _, detail = where.partition(" — ")
        return f"- {code(loc)} {code(what + ')')}" + (f" — {code(detail)}" if detail else "")
    m = LOCATION.match(text)
    if m:
        loc, rest = m.groups()
        name, _, detail = rest.partition(" — ")
        return f"- {code(loc)} {code(name)}" + (f" — {code(detail)}" if detail else "")
    return f"- {code(text)}"


def _reasons(reasons: list[dict]) -> list[str]:
    lines: list[str] = []
    for kind, title in KIND_TITLE.items():
        group = [r for r in reasons if r["kind"] == kind]
        if not group:
            continue
        if kind == "holdout_test":
            n = len(group)
            lines += ["", f"**{title}** ({n})", "",
                      f"- {n} test{'s' if n != 1 else ''} funcional{'es' if n != 1 else ''} reservado{'s' if n != 1 else ''} "
                      f"falla{'n' if n != 1 else ''}; el detalle queda reservado."]
            continue
        rows = [reason_line(r) for r in group]
        if kind == "attack_hidden":
            # Varios ataques a la misma ruta (otro usuario, otro recurso) se cuentan juntos.
            seen: dict[str, int] = {}
            for row in rows:
                seen[row] = seen.get(row, 0) + 1
            rows = [row + (f" ×{n}" if n > 1 else "") for row, n in seen.items()]
        lines += ["", f"**{title}** ({len(group)})", ""] + rows[:MAX_PER_KIND]
        if len(rows) > MAX_PER_KIND:
            lines.append(f"- … y {len(rows) - MAX_PER_KIND} más (ver el reporte JSON del job)")
    return lines


def markdown(report: dict) -> str:
    """Cuerpo del comentario del PR. Termina con `MARKER` para que la Action lo encuentre y lo edite."""
    outcome = report["outcome"]
    lines = [f"### Cachai: {OUTCOME_TITLE[outcome]}", "",
             f"PR `{report['pr'][:10]}` sobre base `{report['base'][:10]}`, modo `{report['mode']}`."]
    if report["mode"] in MODE_DESC:
        lines += ["", f"**Cómo se evaluó:** {MODE_DESC[report['mode']]}"]
    if report["checked"]:
        lines += ["", "**Comprobado**", ""] + [f"- {c}" for c in report["checked"]]
    if outcome == "incomplete":
        lines += ["", "No se detectó un problema, pero faltó evidencia: sin evidencia no hay aprobado."]
    lines += _reasons(report["reasons"])
    if report["notes"]:
        lines += ["", "**Notas** (no bloquean)", ""] + [f"- {code(n)}" for n in report["notes"]]
    lines += ["", "<details><summary>Reproducir en local lo visible</summary>", "",
              "```sh", repro_command(report), "```", "",
              "Corre la app y el evaluador en contenedores separados con los tests de la rama base y los ataques "
              "visibles. Los ataques y tests reservados no se reproducen en local: por eso miden algo que el autor "
              "del PR no pudo ver.", "", "</details>", "", MARKER]
    return "\n".join(lines) + "\n"
