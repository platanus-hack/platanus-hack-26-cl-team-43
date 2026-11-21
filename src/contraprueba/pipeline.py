"""Orquestador: revisa un PR y devuelve la decisión con su evidencia.

Cuatro modos sobre el mismo PR:

- `ci`: línea base. Corre pytest con los tests y la configuración del PR, en
  el mismo contenedor que su código. Es lo que hace un CI normal.
- `restore`: línea base. Repone los tests, la configuración y los hooks de la
  rama base, pero pytest sigue en el mismo contenedor que el código.
- `visible`: línea base para parches de seguridad. Evalúa desde otro
  contenedor, pero solo con los ataques que el parchador vio: es la validación
  que el parchador puede hacerse a sí mismo.
- `full`: Contraprueba. La app corre en un contenedor y el evaluador, con los
  tests de la base, en otro. Suma los ataques y los tests funcionales
  reservados y la revisión de los cambios que tocan la evaluación.

Todo se lee del repo por hash de commit. La configuración (`.contraprueba.json`),
los tests, los ataques y los reservados vienen de la rama base o de fuera del
repo, nunca del PR. Decide `gate.decide`.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import gitio, integrity, sandbox
from .case import CONFIG_FILE, Target, load_target
from .evaluator import EXIT, SCHEMA
from .gate import Evidence, decide
from .holdout import ATTACK_OUTCOMES, Attack, Split
from .integrity import Flag

MODES = ("ci", "restore", "visible", "full")
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
    # approved | rejected (hay evidencia de un problema) | incomplete (solo faltó evidencia)
    outcome: str = ""

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


def target_from_base(repo: str | Path, base_sha: str) -> Target:
    data = gitio.read_blob(repo, base_sha, CONFIG_FILE)
    if data is None:
        raise ValueError(f"la rama base no tiene {CONFIG_FILE}")
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
        holdout_tests_failed=holdout_failed,
        incomplete=incomplete,
    )


def check(
    repo: str | Path,
    base: str,
    pr: str,
    mode: str = "full",
    target: Target | None = None,
    holdout_tests: Path | None = None,
    budget: float = 120.0,
    plan: AttackPlan | None = None,
) -> CheckResult:
    """Revisa el PR. `plan` se pasa cuando el PR es un parche de seguridad: son
    los ataques que debe bloquear."""
    if mode not in MODES:
        raise ValueError(f"modo desconocido: {mode}")
    base_sha, pr_sha = gitio.resolve(repo, base), gitio.resolve(repo, pr)
    target = target or target_from_base(repo, base_sha)

    with tempfile.TemporaryDirectory(prefix="contraprueba-") as tmp:
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

        if mode == "visible":
            holdout_tests = None
        attacks = () if plan is None else (plan.attacks if mode == "full" else plan.subset(plan.split.visible))
        split = plan.split.to_dict() if plan else None

        def done(result: dict | None, ev: Evidence) -> CheckResult:
            verdict = decide(ev)
            return CheckResult(mode, base_sha, pr_sha, verdict.approved, verdict.reasons, verdict.notes, flags,
                               evaluator=result, split=split, outcome=verdict.outcome)

        flags = integrity.scan(repo, base_sha, pr_sha, target.tests_dir)
        if not gitio.is_ancestor(repo, base_sha, pr_sha):
            return done(None, Evidence(False, integrity_flags=flags, incomplete=["la rama del PR no contiene la punta de la base; hay que actualizarla"]))
        workdir = sandbox.prepare_eval_workdir(base_tree, target.tests_dir, tmp_path / "eval")
        attacks_file = None
        if attacks:
            attacks_file = tmp_path / "attacks.json"
            attacks_file.write_text(json.dumps([a.to_dict() for a in attacks], ensure_ascii=False))
        sandbox.build_evaluator_image()
        try:
            app = sandbox.start_app(pr_tree, target)
        except sandbox.SandboxError as e:
            return done(None, Evidence(False, integrity_flags=flags, incomplete=[f"la app del PR no se pudo construir o levantar: {e}"]))
        try:
            code, result = sandbox.run_evaluator(app, target, workdir, holdout_tests, budget, attacks_file)
        finally:
            sandbox.stop(app)
        ev = evidence_from(result, flags, code, holdout_tests is not None, plan, tuple(a.id for a in attacks))
        return done(result, ev)


def _baseline(mode: str, base_sha: str, pr_sha: str, inside: dict) -> CheckResult:
    reasons = [] if inside["passed"] else ["pytest falló dentro del contenedor" + (" (tiempo agotado)" if inside["timed_out"] else "")]
    outcome = "approved" if inside["passed"] else ("incomplete" if inside["timed_out"] else "rejected")
    return CheckResult(mode, base_sha, pr_sha, inside["passed"], reasons, inside=inside, outcome=outcome)
