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

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import canary, gitio, hidden, integrity, report_md, sandbox
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
             families: tuple[str, ...] | None = None) -> list[Attack]:
    """Ataques del equipo rojo que funcionan contra `app`, de las familias pedidas (por defecto, las activas)."""
    families = tuple(f for f in (families if families is not None else target.attack_families) if f in target.attack_families)
    if not families:
        return []
    inv = tmp / f"inventory-{app.run_id}-{'-'.join(families)}.json"
    inv.write_text(json.dumps(target.inventory, ensure_ascii=False))
    found = [Attack.from_dict(d) for d in sandbox.run_redteam(app, target, inv, budget, families)]
    return [a for a in found if a.family in families]


def compare_red_team(pr_found: list[Attack], base_found: list[Attack]) -> Introduced:
    """Un ataque que entra en el PR y no en la base (bloqueado o ruta inexistente) es introducido.

    Los ids del equipo rojo salen de la ruta, el recurso y quien ataca, así que
    el mismo ataque tiene el mismo id contra las dos apps.
    """
    base_ids = {a.id for a in base_found}
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
        if len(ids) != len(set(ids)) or visible & reserved or visible | reserved != set(ids) or not visible or not reserved:
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
    """
    if mode not in MODES:
        raise ValueError(f"modo desconocido: {mode}")
    base_sha, pr_sha = gitio.resolve(repo, base), gitio.resolve(repo, pr)
    target = target or target_from_base(repo, base_sha)

    with tempfile.TemporaryDirectory(prefix="cachai-") as tmp:
        tmp_path = Path(tmp)
        pr_tree = gitio.export_tree(repo, pr_sha, tmp_path / "pr")

        if mode == "ci":
            inside = sandbox.run_pytest_inside(pr_tree, target.tests_dir, budget)
            return _baseline(mode, base_sha, pr_sha, inside)

        base_tree = gitio.export_tree(repo, base_sha, tmp_path / "base")
        if mode == "restore":
            sandbox.restore_eval_files(pr_tree, base_tree, target.tests_dir)
            inside = sandbox.run_pytest_inside(pr_tree, target.tests_dir, budget)
            return _baseline(mode, base_sha, pr_sha, inside)

        # Sin la clave, el modo visible es la revisión local del dev: sigue sin el
        # paquete en vez de quedar incompleto. En full, la falta de clave sí lo deja incompleto.
        local_visible = (allow_missing_hidden and mode == "visible" and target.hidden is not None
                         and not os.environ.get(target.hidden.key_env))
        if plan is None and holdout_tests is None and target.hidden is not None and not local_visible:
            try:
                plan, holdout_tests = load_hidden(repo, base_sha, target, tmp_path / "reservado")
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
            return CheckResult(mode, base_sha, pr_sha, verdict.approved, verdict.reasons, notes, flags,
                               evaluator=result, split=split, outcome=verdict.outcome, typed=verdict.typed,
                               secrets=reserved_secrets(plan if mode == "full" else None, holdout_tests, result))

        spec = target.app
        eval_files = (spec.compose.file,) if spec.compose else ()
        app_files = (spec.dockerfile,) if spec.dockerfile else ()
        flags = integrity.scan(repo, base_sha, pr_sha, target.tests_dir, eval_files, app_files)
        compose_text = gitio.read_blob(repo, base_sha, spec.compose.file) if spec.compose else None
        if spec.compose and compose_text is None:
            return done(None, Evidence(False, integrity_flags=flags, incomplete=[f"la rama base no tiene {spec.compose.file}"]))
        if not gitio.is_ancestor(repo, base_sha, pr_sha):
            return done(None, Evidence(False, integrity_flags=flags, incomplete=["la rama del PR no contiene la punta de la base; hay que actualizarla"]))
        workdir = sandbox.prepare_eval_workdir(base_tree, target.tests_dir, tmp_path / "eval")
        attacks_file = None
        if attacks:
            attacks_file = tmp_path / "attacks.json"
            attacks_file.write_text(json.dumps([a.to_dict() for a in attacks], ensure_ascii=False))
        sandbox.build_evaluator_image()
        hunt = mode == "full" and bool(target.inventory and target.attack_families)
        # Marcas canario nuevas en cada corrida: la semilla las escribe y el equipo rojo las busca.
        canaries = canary.generate(canary.names(target.inventory)) if target.inventory else {}
        attack_target = replace(target, inventory=canary.resolve(target.inventory, canaries)) if canaries else target
        seed_kw = {"seed_context": base_tree, "seed_env": canary.env(canaries)} if target.seed else {}
        pr_found: list[Attack] = []
        red_errors: list[str] = []
        try:
            app = sandbox.start_app(pr_tree, target, compose_text, **seed_kw)
        except sandbox.SandboxError as e:
            return done(None, Evidence(False, integrity_flags=flags, incomplete=[f"la app del PR no se pudo construir o levantar: {e}"]))
        try:
            if hunt:
                # Antes que los tests, con la app recién levantada, igual que la de la base.
                # Las escrituras cambian datos: van después de los tests, para no ensuciar la evaluación.
                reads = tuple(f for f in target.attack_families if f != "writes")
                try:
                    pr_found = red_team(app, attack_target, tmp_path, budget / 2, reads)
                except (sandbox.SandboxError, ValueError) as e:
                    red_errors.append(f"el equipo rojo no terminó contra la app del PR: {e}")
            code, result = sandbox.run_evaluator(app, target, workdir, holdout_tests, budget, attacks_file)
            if hunt and not red_errors and "writes" in target.attack_families:
                try:
                    pr_found += red_team(app, attack_target, tmp_path, budget / 2, ("writes",))
                except (sandbox.SandboxError, ValueError) as e:
                    red_errors.append(f"el equipo rojo no terminó las escrituras contra la app del PR: {e}")
        finally:
            sandbox.stop(app)
        intro = Introduced(incomplete=red_errors)
        if hunt and not red_errors:
            try:
                base_app = sandbox.start_app(base_tree, target, compose_text, **seed_kw)
                try:
                    base_found = red_team(base_app, attack_target, tmp_path, budget / 2)
                finally:
                    sandbox.stop(base_app)
                intro = compare_red_team(pr_found, base_found)
            except (sandbox.SandboxError, ValueError) as e:
                intro = Introduced(incomplete=[f"el equipo rojo no terminó contra la app de la base: {e}"])
        ev = evidence_from(result, flags, code, holdout_tests is not None, plan, tuple(a.id for a in attacks))
        ev.incomplete += intro.incomplete
        ev.introduced_attacks += [describe_introduced(a, attack_target.inventory or {}) for a in intro.introduced]
        r = done(result, ev)
        r.notes += not_external_notes(result)
        r.introduced = [a.to_dict() for a in intro.introduced]
        if intro.existing:
            r.notes.append(existing_note(intro.existing))
        return r


def _baseline(mode: str, base_sha: str, pr_sha: str, inside: dict) -> CheckResult:
    reasons = [] if inside["passed"] else ["pytest falló dentro del contenedor" + (" (tiempo agotado)" if inside["timed_out"] else "")]
    outcome = "approved" if inside["passed"] else ("incomplete" if inside["timed_out"] else "rejected")
    return CheckResult(mode, base_sha, pr_sha, inside["passed"], reasons, inside=inside, outcome=outcome)
