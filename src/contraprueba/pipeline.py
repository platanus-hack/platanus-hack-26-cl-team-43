"""Orquestador: revisa un PR y devuelve la decisión con su evidencia.

Cuatro modos sobre el mismo PR:

- `ci`: línea base. Corre pytest con los tests y la configuración del PR, en
  el mismo contenedor que su código. Es lo que hace un CI normal.
- `restore`: línea base. Repone los tests, la configuración y los hooks de la
  rama base, pero pytest sigue en el mismo contenedor que el código.
- `bot`: la validación del bot de seguridad. Mismo contenedor separado que
  `full`, pero solo con los ataques visibles: sin revisión de integridad ni
  tests reservados. Es lo que el bot ve antes de proponer el parche.
- `full`: Contraprueba. La app corre en un contenedor y el evaluador, con los
  tests de la base, en otro. Además se revisan los cambios que tocan la
  evaluación y, en los PR que parchean la falla, se ejecutan los ataques
  reservados y los tests funcionales reservados. Decide `gate.decide`.

Todo se lee del repo por hash de commit. La configuración (`.contraprueba.json`),
los tests y los reservados vienen de la rama base o de fuera del repo, nunca
del PR.
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
from .holdout import Attack
from .integrity import Flag

MODES = ("ci", "restore", "bot", "full")


@dataclass
class CheckResult:
    mode: str
    base: str
    pr: str
    approved: bool
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    evaluator: dict | None = None  # JSON del evaluador (modos bot y full)
    inside: dict | None = None  # resultado de pytest dentro del contenedor (modos ci y restore)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "base": self.base,
            "pr": self.pr,
            "approved": self.approved,
            "reasons": self.reasons,
            "notes": self.notes,
            "flags": [f.__dict__ for f in self.flags],
            "evaluator": self.evaluator,
            "inside": self.inside,
        }

    def to_text(self) -> str:
        lines = [f"{'APROBADO' if self.approved else 'RECHAZADO'}  (modo {self.mode}, PR {self.pr[:10]} sobre base {self.base[:10]})"]
        lines += [f"  - {r}" for r in self.reasons]
        lines += [f"  · nota: {n}" for n in self.notes]
        return "\n".join(lines)


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


def evidence_from(
    result: object, flags: list[Flag], exit_code: int | None = 0, expect_holdout: bool = False, expect_attacks: bool = False
) -> Evidence:
    """Traduce la salida del evaluador a evidencia.

    Solo hay aprobación posible si el evaluador dijo `ok`, salió con 0 y cada
    suite obligatoria está presente, completa y aprobada, y, si se esperaban
    ataques, su resultado llegó completo. Todo lo demás es un fallo con detalle
    o una evaluación incompleta; nunca un éxito por omisión.
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

    attacks = result.get("attacks")
    confirmed: list[str] = []
    if attacks is None:
        if expect_attacks:
            incomplete.append("ataques reservados sin resultado")
    else:
        try:
            confirmed = [str(a) for a in attacks["confirmed"]]
            if attacks["complete"] is not True:
                incomplete.append("ataques reservados sin resultado")
        except (KeyError, TypeError):
            incomplete.append("resultado de ataques mal formado")

    rejected = bool(base_failed or holdout_failed or confirmed)
    if status == "ok" and (rejected or not base_ok or not holdout_ok):
        incomplete.append("el evaluador dijo ok pero su detalle no lo respalda")
    if status != "ok" and not rejected and not incomplete:
        incomplete.append(f"el evaluador no aprobó (estado {status}) y no informa por qué")
    return Evidence(
        base_tests_passed=base_ok,
        base_tests_failed=base_failed,
        integrity_flags=flags,
        holdout_attacks_succeeded=confirmed,
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
    attacks: list[Attack] | None = None,
) -> CheckResult:
    if mode not in MODES:
        raise ValueError(f"modo desconocido: {mode}")
    if mode == "bot" and not attacks:
        raise ValueError("el modo bot necesita ataques")
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

        bot = mode == "bot"
        flags = [] if bot else integrity.scan(repo, base_sha, pr_sha, target.tests_dir)
        holdout_tests = None if bot else holdout_tests
        if not gitio.is_ancestor(repo, base_sha, pr_sha):
            ev = Evidence(False, integrity_flags=flags, incomplete=["la rama del PR no contiene la punta de la base; hay que actualizarla"])
            return _full(mode, base_sha, pr_sha, flags, None, ev)
        workdir = sandbox.prepare_eval_workdir(base_tree, target.tests_dir, tmp_path / "eval")
        attacks_file = None
        if attacks:
            attacks_file = tmp_path / "attacks.json"
            attacks_file.write_text(json.dumps([a.to_dict() for a in attacks]))
        sandbox.build_evaluator_image()
        try:
            app = sandbox.start_app(pr_tree, target)
        except sandbox.SandboxError as e:
            ev = Evidence(False, integrity_flags=flags, incomplete=[f"la app del PR no se pudo construir o levantar: {e}"])
            return _full(mode, base_sha, pr_sha, flags, None, ev)
        try:
            code, result = sandbox.run_evaluator(app, target, workdir, holdout_tests, budget, attacks_file)
        finally:
            sandbox.stop(app)
        ev = evidence_from(result, flags, code, holdout_tests is not None, bool(attacks))
        return _full(mode, base_sha, pr_sha, flags, result, ev)


def _baseline(mode: str, base_sha: str, pr_sha: str, inside: dict) -> CheckResult:
    reasons = [] if inside["passed"] else ["pytest falló dentro del contenedor" + (" (tiempo agotado)" if inside["timed_out"] else "")]
    return CheckResult(mode, base_sha, pr_sha, inside["passed"], reasons, inside=inside)


def _full(mode: str, base_sha: str, pr_sha: str, flags: list[Flag], result: dict | None, ev: Evidence) -> CheckResult:
    verdict = decide(ev)
    return CheckResult(mode, base_sha, pr_sha, verdict.approved, verdict.reasons, verdict.notes, flags, evaluator=result)
