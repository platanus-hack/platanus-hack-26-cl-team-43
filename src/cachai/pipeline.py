"""Orquestador: revisa un PR y devuelve la decisión con su evidencia.

Cuatro modos sobre el mismo PR:

- `ci`: línea base. Corre pytest con los tests y la configuración del PR, en
  el mismo contenedor que su código. Es lo que hace un CI normal.
- `restore`: línea base. Repone los tests, la configuración y los hooks de la
  rama base, pero pytest sigue en el mismo contenedor que el código.
- `visible`: línea base para parches de seguridad. Evalúa desde otro
  contenedor, pero solo con los ataques que el parchador vio: es la validación
  que el parchador puede hacerse a sí mismo.
- `full`: Cachai. La app corre en un contenedor y el evaluador, con los
  tests de la base, en otro. Suma los ataques y los tests funcionales
  reservados y la revisión de los cambios que tocan la evaluación.

Todo se lee del repo por hash de commit. La configuración (`.cachai.json`),
los tests, los ataques y los reservados vienen de la rama base o de fuera del
repo, nunca del PR. Decide `gate.decide`.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from . import canary, gitio, hidden, integrity, report_md, routes, sandbox, static
from .case import CONFIG_FILE, ConfigError, Target, load_target
from .evaluator import EXIT, SCHEMA
from .gate import Evidence, Reason, decide
from .holdout import ATTACK_OUTCOMES, Attack, Split
from .integrity import Flag

MODES = ("ci", "restore", "visible", "full")
REPORT_VERSION = 2
REDACTED = "[reservado]"
MAX_SHOWN = 6  # motivos que caben en pantalla en la salida en colores


@dataclass
class CheckResult:
    mode: str
    base: str
    pr: str
    approved: bool
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    evaluator: dict | None = None  # JSON del evaluador (modos visible y full)
    inside: dict | None = None  # resultado de pytest dentro del contenedor (modos ci y restore)
    split: dict | None = None  # qué ataques vio el parchador y cuáles quedaron reservados
    introduced: list[dict] | None = None  # ataques que entran en el PR y no en la base (modo full)
    # approved | rejected (hay evidencia de un problema) | incomplete (solo faltó evidencia)
    outcome: str = ""
    typed: list[Reason] = field(default_factory=list)  # motivos con su tipo, para el reporte v2
    # Atajo «solo documentación»: los .md que cambia el PR (aprobado sin levantar nada).
    docs_only: list[str] | None = None
    # Segundos por fase de la revisión (`total` incluido). No cambia el veredicto.
    timings: dict[str, float] = field(default_factory=dict, compare=False)
    # Grupos reservados que quedan por familia y señales de quema (modo full con ataques). Ver `reserve_status`.
    reserve: dict | None = None
    # Textos del material reservado que el reporte público nunca muestra.
    secrets: frozenset[str] = field(default_factory=frozenset, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.outcome:
            self.outcome = "approved" if self.approved else "rejected"

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "base": self.base,
            "pr": self.pr,
            "approved": self.approved,
            "outcome": self.outcome,
            "reasons": self.reasons,
            "notes": self.notes,
            "flags": [f.__dict__ for f in self.flags],
            "evaluator": self.evaluator,
            "inside": self.inside,
            "split": self.split,
            "introduced": self.introduced,
            "docs_only": self.docs_only,
            "timings": self.timings,
            "reserve": self.reserve,
        }

    def to_text(self, color: bool = False) -> str:
        """Veredicto y motivos. Con `color`, el veredicto va en un bloque verde o rojo para leerlo de lejos."""
        word = "APROBADO" if self.approved else "RECHAZADO"
        where = f"(modo {self.mode}, PR {self.pr[:10]} sobre base {self.base[:10]})"
        if not color:
            lines = [f"{word}  {where}"]
            lines += [f"  - {r}" for r in self.reasons]
            lines += [f"  · nota: {n}" for n in self.notes]
            return "\n".join(lines)
        block = "\033[1;97;42m" if self.approved else "\033[1;97;41m"
        mark = "\033[32m" if self.approved else "\033[31m"
        lines = ["", f"{block}  {word}  \033[0m  \033[2m{where}\033[0m", ""]
        lines += [f"  {mark}✗\033[0m {r}" for r in self.reasons[:MAX_SHOWN]]
        if len(self.reasons) > MAX_SHOWN:
            lines.append(f"  \033[2m… y {len(self.reasons) - MAX_SHOWN} motivos más (--json los muestra todos)\033[0m")
        if self.approved:
            lines += [f"  {mark}✓\033[0m {line}" for line in self.checked()]
        lines += [f"  \033[2m· nota: {n}\033[0m" for n in self.notes]
        return "\n".join(lines)

    def checked(self) -> list[str]:
        """Qué se comprobó, para mostrar junto a un aprobado."""
        return checked_lines(self.to_dict())

    def report(self) -> dict:
        """Reporte v2 (`schema/report.schema.json`): lo que la Action sube y comenta en el PR.

        Todo el reporte es público: los motivos de un reservado llevan solo
        familia y ruta, y antes de armar el texto se borra de los motivos, las
        notas y lo comprobado cualquier texto del material reservado (ids,
        pedidos, marcas, nombres de tests) que se haya colado. El texto fijo del
        comentario se agrega después y `report_md.ensure_intact` lo verifica.
        El detalle completo queda en `to_dict`, para quien tiene el material.
        """
        typed = self.typed or [Reason("incomplete" if self.outcome == "incomplete" else "test_base", r, r) for r in self.reasons]
        reasons = []
        for r in typed:
            item = {"kind": r.kind, "text": r.public}
            if r.family:
                item["family"] = r.family
            if r.route:
                item["route"] = r.route
            reasons.append(item)
        report = {
            "schema_version": REPORT_VERSION,
            "mode": self.mode,
            "base": self.base,
            "pr": self.pr,
            "approved": self.approved,
            "outcome": self.outcome,
            # Solo los campos que vienen de datos se redactan, y antes de armar el texto: el texto fijo
            # del comentario (títulos, comando, marcador) no se toca aunque un secreto coincida con él.
            "checked": _redact(self.checked(), self.secrets),
            # De cada motivo se redacta lo que viene de datos; `kind` es un valor fijo del esquema y no se toca:
            # un texto reservado corto (p. ej. «test») no puede romperlo y hacer que el motivo se pierda.
            "reasons": [{k: (v if k == "kind" else _redact(v, self.secrets)) for k, v in r.items()} for r in reasons],
            "notes": _redact(list(self.notes), self.secrets),
        }
        if self.timings:
            report["timings"] = dict(self.timings)
        if self.reserve is not None:
            # Solo conteos, nombres de familia y señales HMAC: nada del paquete en claro.
            report["reserve"] = self.reserve
        report["public"] = {"summary": report_md.summary(report), "markdown": report_md.markdown(report)}
        return report


def checked_lines(report: dict) -> list[str]:
    """Qué se comprobó en una corrida aprobada, leído de su reporte.

    Solo afirma lo que el reporte respalda: una corrida no aprobada no devuelve
    nada, y un reporte aprobado cuyo detalle no cierra (una suite sin aprobar,
    un ataque que no quedó bloqueado) es un error, no una lista de aciertos.
    """
    if report.get("approved") is not True or report.get("outcome", "approved") != "approved":
        return []
    docs = report.get("docs_only")
    if docs is not None:
        if (not docs or not all(isinstance(d, str) and is_doc(d) for d in docs)
                or report.get("evaluator") is not None or report.get("inside") is not None):
            raise ValueError("reporte contradictorio: atajo de documentación con archivos que no son .md o con evaluación")
        return [f"el PR solo cambia documentación ({len(docs)} archivo{'s' if len(docs) != 1 else ''} .md): no se levantó la app"]
    inside = report.get("inside")
    if inside is not None:
        if inside.get("passed") is not True:
            raise ValueError("reporte contradictorio: aprobado sin que pytest pasara")
        return ["pytest salió con 0 dentro del mismo contenedor que el código"]
    ev = report.get("evaluator") or {}
    suites = ev.get("suites") or {}
    base, holdout = suites.get("base"), suites.get("holdout")
    if not base or base.get("ok") is not True or ev.get("status") != "ok":
        raise ValueError("reporte contradictorio: aprobado sin los tests de la base en verde")
    out = [f"{base['passed']} tests de la rama base pasan desde otro contenedor"]
    if holdout is not None:
        if holdout.get("ok") is not True:
            raise ValueError("reporte contradictorio: aprobado con tests reservados sin aprobar")
        out.append(f"{holdout['passed']} tests funcionales reservados pasan")
    attacks, split = ev.get("attacks"), report.get("split")
    if attacks is not None:
        results = attacks.get("results") or []
        if not split or attacks.get("complete") is not True or not results or any(r.get("outcome") != "blocked" for r in results):
            raise ValueError("reporte contradictorio: aprobado con ataques sin bloquear o sin resultado")
        reserved = set(split["reserved"])
        held = sum(r["id"] in reserved for r in results)
        out.append(f"{len(results)} ataques bloqueados" + (f", {held} de ellos reservados" if held else " (solo los que vio el parchador)"))
    return out


def _redact(value: object, secrets: frozenset[str]) -> object:
    """Reemplaza en todo el reporte cada texto del material reservado. Los más
    largos primero, para no dejar restos de uno que contiene a otro."""
    if not secrets:
        return value
    ordered = sorted(secrets, key=len, reverse=True)

    def walk(v: object) -> object:
        if isinstance(v, str):
            for s in ordered:
                v = v.replace(s, REDACTED)
            return v
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(value)


def reserved_secrets(plan: "AttackPlan | None", holdout_tests: Path | None, result: object) -> frozenset[str]:
    """Textos que identifican el material reservado de esta corrida."""
    out: set[str] = set()
    if plan:
        for a in plan.subset(plan.split.reserved):
            out |= a.secrets()
    if holdout_tests and holdout_tests.is_dir():
        for f in holdout_tests.rglob("*.py"):
            out |= {f.name, f.stem}
    suites = result.get("suites") if isinstance(result, dict) else None
    holdout = suites.get("holdout") if isinstance(suites, dict) else None
    if isinstance(holdout, dict):
        for key in ("failed", "skipped"):
            out |= {str(x) for x in holdout.get(key) or []}
        for f in holdout.get("failures") or []:
            if isinstance(f, dict):
                out |= {str(f.get(k)) for k in ("nodeid", "file", "message") if f.get(k)}
                out.add(str(f.get("nodeid", "")).rsplit("::", 1)[-1])
    return frozenset(x for x in out if len(x) >= 4)


@dataclass
class Introduced:
    """Resultado del modo «vulnerabilidad introducida»: el equipo rojo contra el PR y contra la base."""

    introduced: list[Attack] = field(default_factory=list)  # entran en el PR y no en la base
    existing: list[Attack] = field(default_factory=list)  # ya entraban en la base
    incomplete: list[str] = field(default_factory=list)


def who(attack: Attack, inventory: dict) -> str:
    """Usuario del inventario que hace el pedido del ataque."""
    headers = attack.request.get("headers") or {}
    return next((u for u, h in (inventory.get("users") or {}).items() if h == headers), "?")


VARIANT_TEXT = {"none": "sin credencial", "empty": "con la credencial vacía", "invalid": "con un token inválido"}


def anonymous_variant(attack: Attack, inventory: dict) -> str:
    """Cómo pidió un ataque `authn`: la variante del equipo rojo con esos headers."""
    from .redteam import Inventory, anonymous_headers

    headers = attack.request.get("headers") or {}
    try:
        variants = anonymous_headers(Inventory.from_dict(inventory))
    except ValueError:
        variants = {}
    name = next((v for v, h in variants.items() if h == headers), None)
    return f"sin sesión válida ({VARIANT_TEXT[name]})" if name else "sin sesión válida"


def describe_introduced(attack: Attack, inventory: dict) -> str:
    """Evidencia de una vulnerabilidad introducida: pedido, usuario y marca que recibió."""
    pred = attack.succeeded_if
    marks = ", ".join(pred.body_contains_any + pred.body_contains)
    if attack.family == "authn":
        return f"authn: {attack.request['method']} {attack.request['path']} {anonymous_variant(attack, inventory)} recibe datos de un usuario ({marks})"
    return f"{attack.family}: {attack.request['method']} {attack.request['path']} como {who(attack, inventory)} recibe datos ajenos ({marks})"


def red_team(app: "sandbox.AppHandle", target: Target, tmp: Path, budget: float,
             families: tuple[str, ...] | None = None, tree: Path | None = None,
             info: dict | None = None) -> list[Attack]:
    """Ataques del equipo rojo que funcionan contra `app`, de las familias pedidas (por defecto, las activas).

    `tree` es el código de esa app: sus rutas se leen por análisis estático y se
    atacan aunque el OpenAPI no las publique.
    """
    families = tuple(f for f in (families if families is not None else target.attack_families) if f in target.attack_families)
    if not families:
        return []
    tag = f"{app.run_id}-{'-'.join(families)}"
    inv = tmp / f"inventory-{tag}.json"
    inv.write_text(json.dumps(target.inventory, ensure_ascii=False))
    routes_file = None
    if tree is not None:
        routes_file = tmp / f"routes-{tag}.json"
        routes_file.write_text(json.dumps([r.to_dict() for r in routes.extract(tree)], ensure_ascii=False))
    found = [Attack.from_dict(d) for d in sandbox.run_redteam(app, target, inv, budget, families, routes_file, info)]
    return [a for a in found if a.family in families]


def compare_red_team(pr_found: list[Attack], base_found: list[Attack] | set[str]) -> Introduced:
    """Un ataque que entra en el PR y no en la base (bloqueado o ruta inexistente) es introducido.

    Los ids del equipo rojo salen de la ruta, el recurso y quien ataca, así que
    el mismo ataque tiene el mismo id contra las dos apps. `base_found` puede
    ser la lista de ataques o, de la caché, solo sus ids.
    """
    base_ids = {a if isinstance(a, str) else a.id for a in base_found}
    return Introduced(
        introduced=[a for a in pr_found if a.id not in base_ids],
        existing=[a for a in pr_found if a.id in base_ids],
    )


def not_external_notes(result: object) -> list[str]:
    """Tests de la base que usan el código de la app y no se pueden evaluar desde otro contenedor."""
    suites = result.get("suites") if isinstance(result, dict) else None
    base = suites.get("base") if isinstance(suites, dict) else None
    items = (base or {}).get("not_external") or [] if isinstance(base, dict) else []
    if not items:
        return []
    names = ", ".join(str(x.get("nodeid", "")).rsplit("::", 1)[-1] for x in items[:5]) + (f" y {len(items) - 5} más" if len(items) > 5 else "")
    return [f"{len(items)} tests de la base no se evaluaron desde afuera porque usan el código de la app ({names}); "
            "no aprueban ni fallan: ver docs/tests-por-http.md"]


def existing_note(existing: list[Attack]) -> str:
    routes = sorted({a.public_route() for a in existing})
    shown = ", ".join(routes[:4]) + (f" y {len(routes) - 4} más" if len(routes) > 4 else "")
    return f"{len(existing)} vulnerabilidades que ya estaban en la base siguen abiertas; no bloquean este PR ({shown})"


@dataclass(frozen=True)
class AttackPlan:
    """Ataques que el PR debe bloquear y cuáles de ellos son reservados."""

    attacks: tuple[Attack, ...]
    split: Split

    def __post_init__(self) -> None:
        ids = [a.id for a in self.attacks]
        visible, reserved = set(self.split.visible), set(self.split.reserved)
        # Sin reservados es válido: todos se quemaron. El reporte lo avisa como nota.
        if len(ids) != len(set(ids)) or visible & reserved or visible | reserved != set(ids) or not visible:
            raise ValueError("la selección visible/reservada no coincide con los ataques")
        groups = {a.id: a.group_key for a in self.attacks}
        if {groups[i] for i in visible} & {groups[i] for i in reserved}:
            raise ValueError("un grupo de ataques no puede quedar repartido entre visibles y reservados")

    def subset(self, ids: tuple[str, ...]) -> tuple[Attack, ...]:
        wanted = set(ids)
        return tuple(a for a in self.attacks if a.id in wanted)

    def describe(self, attack_id: str) -> str:
        return next(a.describe() for a in self.attacks if a.id == attack_id)

    def public(self, attack_id: str) -> tuple[str, str]:
        a = next(a for a in self.attacks if a.id == attack_id)
        return a.family, a.public_route()


def reserve_status(plan: AttackPlan, confirmed: list[str] | set[str], base: str, key: str | None = None) -> tuple[dict, list[str]]:
    """Cuántos grupos reservados quedan por familia después de esta corrida, y sus notas.

    Un reservado confirmado sale en el comentario del PR con su familia y su
    ruta: desde ese momento su grupo cuenta como visto y ya no se cuenta entre
    los que quedan. Con la clave del paquete (`key`), el reporte lleva una señal
    opaca por grupo revelado (`hidden.burn_token`), con la que quien mantiene el
    repo lo quema (`cachai hidden burn --report`). Sin clave (ataques de un caso
    o de `--attacks`), no hay paquete que quemar y no hay señales.
    """
    from .holdout import reserved_groups

    reserved = plan.subset(plan.split.reserved)
    revealed = {a.group_key for a in reserved if a.id in set(confirmed)}
    remaining = {f: len(g - revealed) for f, g in reserved_groups(plan.attacks, plan.split).items()}
    status: dict = {"remaining": remaining, "revealed": len(revealed)}
    if key and revealed:
        status["burn"] = sorted(hidden.burn_token(key, base, g) for g in revealed)
    notes = []
    if revealed:
        n = len(revealed)
        what = "1 grupo" if n == 1 else f"{n} grupos"
        seen = "cuenta como visto y hay que quemarlo" if n == 1 else "cuentan como vistos y hay que quemarlos"
        notes.append(f"este rechazo muestra familia y ruta de {what} de ataques reservados: desde ahora {seen} "
                     "(cachai hidden burn --report)")
    notes.append("grupos de ataques reservados que quedan por familia: " + ", ".join(f"{f} {n}" for f, n in remaining.items()))
    notes += [f"la familia {f} se quedó sin ataques reservados: sus ataques ya no miden si el parche generaliza; "
              "hay que rotarla (cachai hidden rotate)" for f, n in remaining.items() if n == 0]
    return status, notes


def target_from_base(repo: str | Path, base_sha: str) -> Target:
    data = gitio.read_blob(repo, base_sha, CONFIG_FILE)
    if data is None:
        raise ConfigError("", "no existe en la rama base")
    return load_target(data)


def failure_line(f: dict) -> str:
    where = f"{f['file']}:{f['line']}" if f.get("file") and f.get("line") else str(f["nodeid"])
    name = str(f["nodeid"]).rsplit("::", 1)[-1]
    msg = f" — {f['message']}" if f.get("message") else ""
    return f"{where} {name}{msg}"


def _suite_evidence(name: str, suite: object, required: bool) -> tuple[bool, list[str], list[str]]:
    """Devuelve (aprobada, fallos con detalle, motivos de evaluación incompleta) de una suite."""
    if suite is None:
        return (not required, [], [f"{name}: no corrieron"] if required else [])
    try:
        complete, ok, timed_out = suite["complete"], suite["ok"], suite["timed_out"]
        failures, skipped, passed = list(suite["failures"]), list(suite["skipped"]), suite["passed"]
        failed = [failure_line(f) for f in failures]
    except (KeyError, TypeError):
        return False, [], [f"{name}: resultado mal formado"]
    if not all(isinstance(v, bool) for v in (complete, ok, timed_out)) or isinstance(passed, bool) or not isinstance(passed, int):
        return False, [], [f"{name}: resultado mal formado"]
    incomplete = []
    if not complete:
        incomplete.append(f"{name}: no terminaron" + (" (tiempo agotado)" if timed_out else ""))
    if skipped:
        incomplete.append(f"{name}: tests omitidos ({', '.join(map(str, skipped))})")
    if ok and (failed or incomplete or passed < 1):
        incomplete.append(f"{name}: resultado contradictorio")
    if not ok and not failed and not incomplete:
        incomplete.append(f"{name}: la suite no aprobó y no informa por qué")
    return ok and not failed and not incomplete, failed, incomplete


def _attack_evidence(report: object, expected: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Devuelve (ids confirmados, motivos de evaluación incompleta).

    Se recalcula desde los resultados por ataque: cada ataque esperado debe
    tener exactamente un resultado `confirmed` o `blocked` con su status HTTP.
    """
    if not expected:
        return [], ([] if report is None else ["el evaluador corrió ataques que nadie pidió"])
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        return [], ["ataques: no corrieron o su resultado está mal formado"]
    seen: dict[str, str] = {}
    why: dict[str, str] = {}
    problems = []
    for r in report["results"]:
        if not isinstance(r, dict) or r.get("outcome") not in ATTACK_OUTCOMES or not isinstance(r.get("id"), str):
            return [], ["ataques: resultado mal formado"]
        if r["id"] in seen or r["id"] not in expected:
            problems.append(f"ataques: resultado repetido o no pedido ({r['id']})")
            continue
        status = r.get("http_status")
        if r["outcome"] != "error" and (isinstance(status, bool) or not isinstance(status, int)):
            return [], ["ataques: resultado sin status HTTP"]
        seen[r["id"]] = r["outcome"]
        why[r["id"]] = str(r.get("error") or "")[:160]
    missing = [i for i in expected if i not in seen]
    errors = [i for i, o in seen.items() if o == "error"]
    if missing:
        problems.append(f"ataques sin resultado: {', '.join(missing)}")
    if errors:
        shown = "; ".join(f"{i} ({why[i]})" for i in errors[:3])
        more = f" y {len(errors) - 3} más" if len(errors) > 3 else ""
        problems.append(f"{len(errors)} ataques sin veredicto: {shown}{more}")
    if not problems and report.get("complete") is not True:
        problems.append("ataques: el evaluador no los dio por completos")
    return [i for i in expected if seen.get(i) == "confirmed"], problems


def evidence_from(
    result: object,
    flags: list[Flag],
    exit_code: int | None = 0,
    expect_holdout: bool = False,
    plan: AttackPlan | None = None,
    attack_ids: tuple[str, ...] = (),
) -> Evidence:
    """Traduce la salida del evaluador a evidencia.

    Solo hay aprobación posible si el evaluador dijo `ok`, salió con 0, cada
    suite obligatoria está presente, completa y aprobada, y cada ataque pedido
    (`attack_ids`) tiene resultado. Todo lo demás es un fallo con detalle o una
    evaluación incompleta; nunca un éxito por omisión.
    """
    if not isinstance(result, dict):
        return Evidence(False, integrity_flags=flags, incomplete=["salida del evaluador mal formada"])
    incomplete = [str(e) for e in (result.get("errors") or [])]
    status = result.get("status")
    if result.get("schema") != SCHEMA:
        incomplete.append("salida del evaluador con esquema desconocido")
    if status not in EXIT:
        incomplete.append(f"estado del evaluador desconocido: {status!r}")
    elif exit_code != EXIT[status]:
        incomplete.append(f"el código de salida del evaluador ({exit_code}) no coincide con su estado ({status})")
    suites = result.get("suites") if isinstance(result.get("suites"), dict) else {}
    base_ok, base_failed, inc = _suite_evidence("tests de la base", suites.get("base"), True)
    incomplete += inc
    holdout_ok, holdout_failed, inc = _suite_evidence("tests reservados", suites.get("holdout"), expect_holdout)
    incomplete += inc
    confirmed, inc = _attack_evidence(result.get("attacks"), attack_ids)
    incomplete += inc

    reserved = set(plan.split.reserved) if plan else set()
    describe = plan.describe if plan else str
    rejected = bool(base_failed or holdout_failed or confirmed)
    if status == "ok" and (rejected or not base_ok or not holdout_ok):
        incomplete.append("el evaluador dijo ok pero su detalle no lo respalda")
    if status != "ok" and not rejected and not incomplete:
        incomplete.append(f"el evaluador no aprobó (estado {status}) y no informa por qué")
    return Evidence(
        base_tests_passed=base_ok,
        base_tests_failed=base_failed,
        integrity_flags=flags,
        visible_attacks_succeeded=[describe(i) for i in confirmed if i not in reserved],
        holdout_attacks_succeeded=[describe(i) for i in confirmed if i in reserved],
        holdout_attacks_public=[plan.public(i) for i in confirmed if i in reserved] if plan else [],
        holdout_tests_failed=holdout_failed,
        incomplete=incomplete,
    )


def load_hidden(repo: str | Path, base_sha: str, target: Target, dest: Path) -> tuple[AttackPlan | None, Path | None]:
    """Abre el paquete reservado que declara `.cachai.json` de la base.

    Una ruta relativa se lee del commit de la base (nunca del árbol del PR) y la
    clave, de la variable de entorno que nombra la configuración. Lo extraído
    queda en `dest`, que solo se monta en el contenedor del evaluador.
    """
    h = target.hidden
    if h.package.startswith("/"):
        sealed = Path(h.package).read_bytes()
    else:
        sealed = gitio.read_blob(repo, base_sha, h.package)
        if sealed is None:
            raise hidden.HiddenError(f"la rama base no tiene {h.package}")
    material = hidden.open_to_dir(sealed, os.environ.get(h.key_env), dest)
    plan = AttackPlan(material.attacks, material.split) if material.attacks and material.split else None
    return plan, material.holdout_tests


def is_doc(path: str) -> bool:
    """Archivo de documentación para el atajo: termina en `.md` (en minúsculas)."""
    return PurePosixPath(path).suffix == ".md"


# Si algún archivo de la base que no es .md nombra esto, la app o pytest podrían
# leer los .md (plantillas, doctests): el atajo no aplica.
DOC_READERS = r"\.md|markdown|doctest"


def docs_only_files(repo: str | Path, base_sha: str, pr_sha: str, target: Target, flags: list[Flag]) -> list[str] | None:
    """Los .md que cambia el PR si se puede aprobar sin levantar nada; si no, None.

    Un .md no se ejecuta, pero puede cambiar la evaluación si algo lo lee. Por
    eso el atajo exige, además de que todo cambio sea un `.md`:

    - ninguna marca de integridad (un .md dentro de la carpeta de tests la tiene);
    - ningún enlace simbólico entre los .md del PR;
    - la imagen de app por defecto (un Dockerfile o compose del repo puede
      procesar cualquier archivo al construir);
    - que ningún archivo de la base que no sea .md nombre `.md`, `markdown` o
      `doctest`: ni la app lee .md por nombre ni pytest los colecta como doctests.

    Límite: no detecta una app de la base que lea todos los archivos de una
    carpeta sin nombrar su extensión.
    """
    changes = gitio.changed_files(repo, base_sha, pr_sha)
    if not changes or flags or not all(is_doc(path) for _, path in changes):
        return None
    if target.app.dockerfile is not None or target.app.compose is not None:
        return None
    if any(integrity.in_dir(path, target.tests_dir) for _, path in changes):
        return None
    modes = {path: mode for mode, _, _, path in gitio.ls_tree(repo, pr_sha)}
    if any(status != "D" and modes.get(path) not in ("100644", "100755") for status, path in changes):
        return None
    if gitio.grep_mentions(repo, base_sha, DOC_READERS):
        return None
    return sorted(path for _, path in changes)


DOCS_NOTE = ("el PR solo cambia documentación (*.md): se aprueba sin levantar la app ni correr tests ni ataques, "
             "porque ningún archivo de la base lee .md y la app se evalúa igual que la base")


# Caché del equipo rojo contra la base, por sha de la base. Guarda solo los ids
# de los ataques que funcionan contra la base (nunca marcas canario), fuera del
# repo y de todo contenedor. Solo la escriben corridas contra la app de la base.
BASE_HUNT_VERSION = 1


def base_hunt_key(base_sha: str, target: Target, compose_text: bytes | str | None) -> str:
    """Todo lo que determina el resultado del equipo rojo contra la base: el
    commit, la configuración, el compose y el código de Cachai."""
    if isinstance(compose_text, str):
        compose_text = compose_text.encode()
    data = {
        "v": BASE_HUNT_VERSION,
        "base": base_sha,
        "target": target.to_dict(),
        "compose": hashlib.sha256(compose_text).hexdigest() if compose_text is not None else None,
        "code": sandbox.evaluator_tag(),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _base_hunt_file(key: str) -> Path:
    return sandbox.cache_dir() / "rojo-base" / f"{key}.json"


def load_base_hunt(key: str, base_sha: str) -> set[str] | None:
    """Ids cacheados del equipo rojo contra la base, o None si no hay o no se pueden leer."""
    try:
        data = json.loads(_base_hunt_file(key).read_text())
    except (OSError, ValueError):
        return None
    ids = data.get("ids") if isinstance(data, dict) else None
    if data.get("base") != base_sha or not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return None
    return set(ids)


def save_base_hunt(key: str, base_sha: str, ids: set[str]) -> None:
    f = _base_hunt_file(key)
    try:
        f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump({"base": base_sha, "ids": sorted(ids)}, out)
        os.replace(tmp, f)
    except OSError:
        pass  # sin caché la próxima revisión corre el equipo rojo contra la base


def timings_text(timings: dict[str, float]) -> str:
    """`total 4.1 s (app_pr 1.2 s, evaluador 1.9 s, …)`."""
    parts = ", ".join(f"{k} {v:.1f} s" for k, v in timings.items() if k != "total")
    total = timings.get("total")
    return (f"total {total:.1f} s" if total is not None else "") + (f" ({parts})" if parts else "")


class Timer:
    """Segundos por fase. Las fases que corren en paralelo se miden cada una por su lado."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.phases: dict[str, float] = {}

    @contextmanager
    def phase(self, name: str):
        t = time.monotonic()
        try:
            yield
        finally:
            self.phases[name] = round(self.phases.get(name, 0.0) + time.monotonic() - t, 2)

    def done(self) -> dict[str, float]:
        return {**self.phases, "total": round(time.monotonic() - self.started, 2)}


def attacks_open_in_base(base_app: "sandbox.AppHandle", target: Target, attacks_file: Path, budget: float) -> set[str] | None:
    """Ids de los ataques que funcionan contra la app de la base, o None si no se pudo saber."""
    try:
        report = sandbox.run_attacks_only(base_app, target, attacks_file, budget)
    except sandbox.SandboxError:
        return None
    results = report.get("results") if isinstance(report, dict) else None
    if not isinstance(results, list) or report.get("complete") is not True:
        return None
    return {r["id"] for r in results if isinstance(r, dict) and r.get("outcome") == "confirmed"}


def excuse_existing(ev: Evidence, plan: "AttackPlan", attack_ids: tuple[str, ...], pr_confirmed: list[str],
                    base_open: set[str]) -> list[str]:
    """Ataques del paquete reservado que no bloquean este PR. Modifica `ev`.

    El paquete lo arma `cachai init` corriendo el equipo rojo contra la base: si
    la base es vulnerable, sus ataques funcionan contra cualquier PR. Regla:

    - Un ataque que la base bloquea y el PR no, siempre bloquea (el PR lo reabrió).
    - Si el PR cierra al menos uno de los que la base sufre, es un intento de
      arreglo y debe cerrarlos todos, visibles y reservados: así se rechaza el
      parche que solo tapa lo que vio.
    - Si no cierra ninguno, los que ya funcionaban en la base son fallas
      existentes: no son culpa del PR y quedan como nota.
    """
    wanted = set(attack_ids)
    open_base = base_open & wanted
    if open_base - set(pr_confirmed):
        return []  # intento de arreglo: debe cerrar todo
    excused = [i for i in pr_confirmed if i in open_base]
    drop = {plan.describe(i) for i in excused}
    keep = [k for k, d in enumerate(ev.holdout_attacks_succeeded) if d not in drop]
    if len(ev.holdout_attacks_public) == len(ev.holdout_attacks_succeeded):
        ev.holdout_attacks_public = [ev.holdout_attacks_public[k] for k in keep]
    ev.holdout_attacks_succeeded = [ev.holdout_attacks_succeeded[k] for k in keep]
    ev.visible_attacks_succeeded = [d for d in ev.visible_attacks_succeeded if d not in drop]
    return excused


def check(
    repo: str | Path,
    base: str,
    pr: str,
    mode: str = "full",
    target: Target | None = None,
    holdout_tests: Path | None = None,
    budget: float = 120.0,
    plan: AttackPlan | None = None,
    allow_missing_hidden: bool = False,
) -> CheckResult:
    """Revisa el PR. `plan` se pasa cuando el PR es un parche de seguridad: son
    los ataques que debe bloquear.

    `allow_missing_hidden`: solo para la revisión local del dev en modo
    visible. Sin él (el caso por defecto, y siempre en CI), si la base declara
    un paquete reservado y no se puede abrir, la revisión queda incompleta.

    El resultado lleva el tiempo de cada fase en `timings`.
    """
    timer = Timer()
    result = _check(repo, base, pr, mode, target, holdout_tests, budget, plan, allow_missing_hidden, timer)
    result.timings = timer.done()
    return result


def _check(repo: str | Path, base: str, pr: str, mode: str, target: Target | None, holdout_tests: Path | None,
           budget: float, plan: AttackPlan | None, allow_missing_hidden: bool, timer: Timer) -> CheckResult:
    if mode not in MODES:
        raise ValueError(f"modo desconocido: {mode}")
    with timer.phase("preparar"):
        base_sha, pr_sha = gitio.resolve(repo, base), gitio.resolve(repo, pr)
        target = target or target_from_base(repo, base_sha)

    with tempfile.TemporaryDirectory(prefix="cachai-") as tmp:
        tmp_path = Path(tmp)
        with timer.phase("preparar"):
            pr_tree = gitio.export_tree(repo, pr_sha, tmp_path / "pr")

        if mode == "ci":
            with timer.phase("pytest_dentro"):
                inside = sandbox.run_pytest_inside(pr_tree, target.tests_dir, budget)
            return _baseline(mode, base_sha, pr_sha, inside)

        with timer.phase("preparar"):
            base_tree = gitio.export_tree(repo, base_sha, tmp_path / "base")
        if mode == "restore":
            sandbox.restore_eval_files(pr_tree, base_tree, target.tests_dir)
            with timer.phase("pytest_dentro"):
                inside = sandbox.run_pytest_inside(pr_tree, target.tests_dir, budget, trusted_deps=base_tree)
            return _baseline(mode, base_sha, pr_sha, inside)

        # Sin la clave, el modo visible es la revisión local del dev: sigue sin el
        # paquete en vez de quedar incompleto. En full, la falta de clave sí lo deja incompleto.
        local_visible = (allow_missing_hidden and mode == "visible" and target.hidden is not None
                         and not os.environ.get(target.hidden.key_env))
        # Los ataques del paquete se juzgan contra la base (ver `excuse_existing`); los que llegan
        # por `plan` (un parche de seguridad declarado) se deben bloquear todos.
        from_package = False
        burn_key = None  # clave del paquete: firma las señales de quema del reporte
        if plan is None and holdout_tests is None and target.hidden is not None and not local_visible:
            try:
                with timer.phase("preparar"):
                    plan, holdout_tests = load_hidden(repo, base_sha, target, tmp_path / "reservado")
                from_package = plan is not None
                burn_key = os.environ.get(target.hidden.key_env)
            except (hidden.HiddenError, ValueError, OSError) as e:
                msg = f"evaluación incompleta: no se pudo abrir el material reservado ({e})"
                return CheckResult(mode, base_sha, pr_sha, False, [msg], outcome="incomplete", typed=[Reason("incomplete", msg, msg)])
        if mode == "visible":
            holdout_tests = None
        attacks = () if plan is None else (plan.attacks if mode == "full" else plan.subset(plan.split.visible))
        split = plan.split.to_dict() if plan else None

        def done(result: dict | None, ev: Evidence) -> CheckResult:
            verdict = decide(ev)
            notes = verdict.notes + ([f"sin {target.hidden.key_env}: no se abrió el paquete reservado, ni sus ataques visibles"]
                                     if local_visible and plan is None else [])
            reserve = None
            if mode == "full" and plan is not None and isinstance(result, dict):
                confirmed, _ = _attack_evidence(result.get("attacks"), tuple(a.id for a in attacks))
                reserve, reserve_notes = reserve_status(plan, confirmed, base_sha, burn_key)
                notes += reserve_notes
            return CheckResult(mode, base_sha, pr_sha, verdict.approved, verdict.reasons, notes, flags,
                               evaluator=result, split=split, outcome=verdict.outcome, typed=verdict.typed,
                               secrets=reserved_secrets(plan if mode == "full" else None, holdout_tests, result),
                               reserve=reserve)

        spec = target.app
        with timer.phase("integridad"):
            eval_files = (spec.compose.file,) if spec.compose else ()
            app_files = (spec.dockerfile,) if spec.dockerfile else ()
            flags = integrity.scan(repo, base_sha, pr_sha, target.tests_dir, eval_files, app_files)
        compose_text = gitio.read_blob(repo, base_sha, spec.compose.file) if spec.compose else None
        if spec.compose and compose_text is None:
            return done(None, Evidence(False, integrity_flags=flags, incomplete=[f"la rama base no tiene {spec.compose.file}"]))
        if not gitio.is_ancestor(repo, base_sha, pr_sha):
            return done(None, Evidence(False, integrity_flags=flags, incomplete=["la rama del PR no contiene la punta de la base; hay que actualizarla"]))
        if plan is None:
            # Un parche de seguridad (con `plan`) siempre se evalúa: el atajo es para PR normales.
            with timer.phase("integridad"):
                docs = docs_only_files(repo, base_sha, pr_sha, target, flags)
            if docs:
                r = done(None, Evidence(True, integrity_flags=flags))
                r.docs_only = docs
                r.notes.append(DOCS_NOTE)
                return r
        workdir = sandbox.prepare_eval_workdir(base_tree, target.tests_dir, tmp_path / "eval")
        attacks_file = None
        if attacks:
            attacks_file = tmp_path / "attacks.json"
            attacks_file.write_text(json.dumps([a.to_dict() for a in attacks], ensure_ascii=False))
        hunt = mode == "full" and bool(target.inventory and target.attack_families)
        # Marcas canario nuevas en cada corrida: la semilla las escribe y el equipo rojo las busca.
        canaries = canary.generate(canary.names(target.inventory)) if target.inventory else {}
        attack_target = replace(target, inventory=canary.resolve(target.inventory, canaries)) if canaries else target
        seed_kw = {"seed_context": base_tree, "seed_env": canary.env(canaries)} if target.seed else {}
        app_kw = {**seed_kw, "trusted_deps": base_tree}
        attack_ids = tuple(a.id for a in attacks)
        # El paquete sellado por `cachai init` se armó contra la base: sus ataques pueden
        # funcionar ya en la base, y entonces no son culpa de este PR (ver `excuse_existing`).
        needs_base_open = from_package and bool(attacks)
        needs_base = hunt or needs_base_open
        hunt_key = base_hunt_key(base_sha, target, compose_text) if hunt else ""
        cached_base = load_base_hunt(hunt_key, base_sha) if hunt else None
        if cached_base is not None:
            timer.phases["rojo_base"] = 0.0

        def evaluator_image() -> None:
            with timer.phase("imagen_evaluador"):
                sandbox.build_evaluator_image()

        def base_hunt() -> tuple[set[str], set[str] | None]:
            """Equipo rojo contra la app de la base (en paralelo con la evaluación del PR)."""
            with timer.phase("rojo_base"):
                base_app = sandbox.start_app(base_tree, target, compose_text, **app_kw)
                try:
                    found = ({a.id for a in red_team(base_app, attack_target, tmp_path, budget / 2, tree=base_tree)}
                             if hunt else set())
                    base_open = (attacks_open_in_base(base_app, target, attacks_file, budget / 2)
                                 if needs_base_open else None)
                finally:
                    sandbox.stop(base_app)
            if hunt:
                save_base_hunt(hunt_key, base_sha, found)
            return found, base_open

        pr_found: list[Attack] = []
        pr_info: dict = {}
        red_errors: list[str] = []
        # La pool se cierra (esperando a todos) antes de borrar la carpeta temporal.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="cachai") as pool:
            # La imagen del evaluador se prepara mientras se construye y levanta la app del PR.
            eval_ready = pool.submit(evaluator_image)
            try:
                with timer.phase("app_pr"):
                    app = sandbox.start_app(pr_tree, target, compose_text, **app_kw)
            except sandbox.SandboxError as e:
                eval_ready.result()
                return done(None, Evidence(False, integrity_flags=flags, incomplete=[f"la app del PR no se pudo construir o levantar: {e}"]))
            try:
                eval_ready.result()
                base_future = pool.submit(base_hunt) if needs_base and (cached_base is None or needs_base_open) else None
                if hunt:
                    # Antes que los tests, con la app recién levantada, igual que la de la base.
                    # Las escrituras cambian datos: van después de los tests, para no ensuciar la evaluación.
                    reads = tuple(f for f in target.attack_families if f != "writes")
                    try:
                        with timer.phase("rojo_pr"):
                            pr_found = red_team(app, attack_target, tmp_path, budget / 2, reads, pr_tree, pr_info)
                    except (sandbox.SandboxError, ValueError) as e:
                        red_errors.append(f"el equipo rojo no terminó contra la app del PR: {e}")
                with timer.phase("evaluador"):
                    code, result = sandbox.run_evaluator(app, target, workdir, holdout_tests, budget, attacks_file)
                if hunt and not red_errors and "writes" in target.attack_families:
                    try:
                        with timer.phase("rojo_pr"):
                            pr_found += red_team(app, attack_target, tmp_path, budget / 2, ("writes",), pr_tree)
                    except (sandbox.SandboxError, ValueError) as e:
                        red_errors.append(f"el equipo rojo no terminó las escrituras contra la app del PR: {e}")
            finally:
                with timer.phase("parar"):
                    sandbox.stop(app)
            pr_confirmed: list[str] = []
            if from_package and isinstance(result, dict):
                pr_confirmed, _ = _attack_evidence(result.get("attacks"), attack_ids)
            intro = Introduced(incomplete=red_errors)
            base_open: set[str] | None = None
            base_notes: list[str] = []
            if needs_base:
                try:
                    if base_future is None:
                        base_found, base_open = cached_base or set(), None
                    else:
                        base_found, base_open = base_future.result()
                    if hunt and not red_errors:
                        intro = compare_red_team(pr_found, base_found)
                except (sandbox.SandboxError, ValueError) as e:
                    if not red_errors:
                        intro = Introduced(incomplete=[f"el equipo rojo no terminó contra la app de la base: {e}"])
                    if needs_base_open:
                        base_notes.append(f"no se pudieron correr los ataques del paquete contra la base ({e}); "
                                          "se exige bloquearlos todos")
            if needs_base_open and base_open is None and not base_notes:
                base_notes.append("no se pudo saber qué ataques del paquete ya funcionaban en la base; se exige bloquearlos todos")
        ev = evidence_from(result, flags, code, holdout_tests is not None, plan, attack_ids)
        if base_open is not None and plan is not None:
            excused = excuse_existing(ev, plan, attack_ids, pr_confirmed, base_open)
            if excused:
                base_notes.append(f"{len(excused)} ataques del paquete reservado ya funcionaban contra la base y el PR no intenta "
                                  "arreglar ninguno; no bloquean este PR")
        ev.incomplete += intro.incomplete
        ev.introduced_attacks += [describe_introduced(a, attack_target.inventory or {}) for a in intro.introduced]
        r = done(result, ev)
        r.notes += not_external_notes(result)
        if mode == "full":
            # Nota, nunca motivo: el estático da falsos positivos.
            try:
                r.notes += static.scan(repo, base_sha, pr_sha, pr_tree, target.tests_dir, tmp_path)
            except (sandbox.SandboxError, gitio.GitError, OSError) as e:
                r.notes.append(f"análisis estático: no corrió ({e})")
        r.introduced = [a.to_dict() for a in intro.introduced]
        if intro.existing:
            r.notes.append(existing_note(intro.existing))
        off_openapi = pr_info.get("hidden_routes") or []
        if off_openapi:
            shown = ", ".join(f"{h['method']} {h['template']} ({h['source']})" for h in off_openapi[:4])
            more = f" y {len(off_openapi) - 4} más" if len(off_openapi) > 4 else ""
            r.notes.append(f"{len(off_openapi)} rutas del código no están en el OpenAPI de la app; se atacaron igual: {shown}{more}")
        r.notes += base_notes
        return r


def _baseline(mode: str, base_sha: str, pr_sha: str, inside: dict) -> CheckResult:
    reasons = [] if inside["passed"] else ["pytest falló dentro del contenedor" + (" (tiempo agotado)" if inside["timed_out"] else "")]
    outcome = "approved" if inside["passed"] else ("incomplete" if inside["timed_out"] else "rejected")
    return CheckResult(mode, base_sha, pr_sha, inside["passed"], reasons, inside=inside, outcome=outcome)
