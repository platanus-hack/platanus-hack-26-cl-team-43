"""CLI de Cachai.

    cachai init [--repo .] [--yes] [--user alice='Authorization: Bearer tok-alice' ...]
    cachai check --repo <ruta> --base main --pr <rama> [--mode ci|restore|visible|full]
    cachai case <carpeta del caso> [--pr <nombre>|all] [--mode <modo>|all]
    cachai redteam <carpeta del caso> [--write]
    cachai bench [carpeta de casos] [--out results]
    cachai fix <carpeta del caso> [--agent claude|codex] [--runs 3]

`check` revisa un PR de un repo cuya rama base tiene `.cachai.json`. Si
el PR es un parche de seguridad, `--attacks` son los ataques que debe bloquear.
Sin material reservado (`--holdout-tests`) y sin `--mode`, corre en modo
`visible` y avisa que no es el veredicto final: es la revisión local del dev,
que reproduce lo que vio sin que lo reservado salga del CI.
`case` arma el repo de un caso de `cases/` y lo revisa; con `all` imprime la
tabla de modos por PR. `redteam` genera las variantes de ataque del caso y las
confirma contra su rama base. `check` y `case` salen con 0 si todo se aprueba.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import bench, fixer, gitio, hidden, init, pipeline, sandbox
from .case import ConfigError, load_case, materialize
from .holdout import Attack, load_attacks, split_record


def use_color(args: argparse.Namespace) -> bool:
    if getattr(args, "color", "auto") != "auto":
        return args.color == "always"
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def save_report(result: pipeline.CheckResult, args: argparse.Namespace) -> None:
    if getattr(args, "report", None):
        Path(args.report).write_text(json.dumps(result.report(), ensure_ascii=False, indent=2) + "\n")


def _print(result: pipeline.CheckResult, args: argparse.Namespace) -> None:
    save_report(result, args)
    if getattr(args, "public", False):
        # En CI el log lo ve el autor del PR: solo el texto público, sin detalle de los reservados.
        print(json.dumps(result.report(), ensure_ascii=False, indent=2) if args.json else result.report()["public"]["markdown"], end="" if not args.json else "\n")
        return
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) if args.json else result.to_text(use_color(args)))


VISIBLE_WARNING = "faltan los ataques reservados, esto no es el veredicto final"
CI_ENV = ("CI", "GITHUB_ACTIONS")


class ModeError(Exception):
    pass


def hidden_key_available(repo: str, base: str) -> bool:
    """Si `.cachai.json` de la base declara un paquete reservado y su clave está en el entorno."""
    try:
        h = pipeline.target_from_base(repo, gitio.resolve(repo, base)).hidden
    except (ConfigError, gitio.GitError, ValueError, OSError):
        return False
    return h is not None and bool(os.environ.get(h.key_env))


def check_mode(args: argparse.Namespace) -> str:
    """El modo pedido o, si no se pidió, `full` con material reservado a mano
    (tests reservados o la clave del paquete) y `visible` sin él. En CI no se
    adivina: un default que degrada a `visible` aprobaría en silencio lo que
    `full` rechaza."""
    if args.mode:
        return args.mode
    if args.holdout_tests or hidden_key_available(args.repo, args.base):
        return "full"
    if any(os.environ.get(k) for k in CI_ENV):
        raise ModeError("en CI hay que pasar --mode explícito (full para el veredicto final)")
    return "visible"


def visible_banner(color: bool) -> str:
    lines = [f"MODO VISIBLE: {VISIBLE_WARNING}.",
             "El CI suma ataques y tests que nadie ve antes del merge; allá este PR todavía puede caer.",
             "Para el veredicto final, el CI corre --mode full con el material reservado."]
    width = max(len(line) for line in lines) + 4
    if not color:
        return "\n".join(["!" * (width + 2), *(f"!! {line.ljust(width - 4)} !!" for line in lines), "!" * (width + 2)])
    return "\n".join(["", *(f"\033[1;30;43m  {line.ljust(width - 2)}\033[0m" for line in lines), ""])


def curl_command(request: dict, base_url: str) -> str:
    """El pedido como lo manda el evaluador: ruta tal cual (sin normalizar `..` ni
    expandir `[]{}`) y siguiendo redirecciones."""
    method = request["method"]
    cmd = ["curl", "-i", "--path-as-is", "--globoff", "-L"]
    cmd += ["-I"] if method == "HEAD" else ["-X", method]
    headers = request.get("headers") or {}
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    if "json" in request:
        if not any(k.lower() == "content-type" for k in headers):
            cmd += ["-H", "Content-Type: application/json"]
        cmd += ["--data", json.dumps(request["json"], ensure_ascii=False)]
    return " ".join(shlex.quote(c) for c in cmd) + f' "${{BASE_URL:-{base_url}}}"' + shlex.quote(request["path"])


def reproduce_commands(result: pipeline.CheckResult, plan: "pipeline.AttackPlan | None", base_url: Callable[[], str]) -> list[str]:
    """Un curl por cada ataque visible que sigue funcionando, para probarlo contra
    la app local. Nunca uno reservado, aunque el evaluador lo devolviera."""
    if plan is None or not result.evaluator or not result.evaluator.get("attacks"):
        return []
    visible = set(plan.split.visible)
    by_id = {a.id: a for a in plan.attacks}
    shown = [by_id[aid] for aid in result.evaluator["attacks"].get("confirmed", []) if aid in visible and aid in by_id]
    if not shown:
        return []
    url = base_url()  # lee .cachai.json de la base solo si hay algo que reproducir
    return [curl_command(a.request, url) for a in shown]


def local_base_url(repo: str, base_sha: str) -> str:
    try:
        port = pipeline.target_from_base(repo, base_sha).app.port
    except (ConfigError, ValueError, OSError):
        port = 8000
    return f"http://localhost:{port}"


def warn_visible(color: bool) -> None:
    sys.stdout.flush()  # que el aviso no se adelante al veredicto si stdout va a un archivo
    print(visible_banner(color), file=sys.stderr, flush=True)


def cmd_check(args: argparse.Namespace) -> int:
    holdout = Path(args.holdout_tests) if args.holdout_tests else None
    plan = None
    if args.attacks:
        attacks = load_attacks(args.attacks)
        plan = pipeline.AttackPlan(tuple(attacks), split_record(attacks, args.shown, args.seed))
    try:
        mode = check_mode(args)
    except ModeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    # El aviso va a stderr: con --json la salida estándar sigue siendo JSON válido.
    banner_color = args.color == "always" or (args.color == "auto" and sys.stderr.isatty() and not os.environ.get("NO_COLOR"))
    if mode == "visible":
        warn_visible(banner_color)
    try:
        # Solo el dev en su máquina puede revisar sin el paquete reservado; en CI eso deja la revisión incompleta.
        local = mode == "visible" and not any(os.environ.get(k) for k in CI_ENV)
        result = pipeline.check(args.repo, args.base, args.pr, mode, holdout_tests=holdout, budget=args.budget, plan=plan,
                                allow_missing_hidden=local)
    except ConfigError as e:
        print(f"error de configuración en la rama base: {e}", file=sys.stderr)
        return 2
    _print(result, args)
    if mode == "visible":
        if not args.json:
            commands = reproduce_commands(result, plan, lambda: local_base_url(args.repo, result.base))
            if commands:
                print("\nPara reproducir cada ataque visible contra tu app local:")
                print("\n".join(f"  {c}" for c in commands))
        warn_visible(banner_color)
    return 0 if result.approved else 1


def run_case(case, prs: list[str], modes: list[str], budget: float = 120.0) -> dict[tuple[str, str], pipeline.CheckResult]:
    """Revisa los PR de un caso en los modos pedidos. Los parches de seguridad
    reciben además los ataques del caso, con su selección visible/reservada."""
    attacks, split = case.attacks(), case.split()
    plan = pipeline.AttackPlan(tuple(attacks), split) if split else None
    results: dict[tuple[str, str], pipeline.CheckResult] = {}
    with tempfile.TemporaryDirectory(prefix="cachai-caso-") as tmp:
        repo = materialize(case, Path(tmp) / "repo")
        for pr in prs:
            for mode in modes:
                results[pr, mode] = pipeline.check(
                    repo, "main", pr, mode, target=case.target(), holdout_tests=case.holdout_tests_path, budget=budget,
                    plan=plan if case.pr(pr).is_security_patch else None,
                )
    return results


def cmd_redteam(args: argparse.Namespace) -> int:
    case = load_case(args.case)
    if not case.inventory:
        print("el caso no tiene inventario", file=sys.stderr)
        return 2
    sandbox.build_evaluator_image()
    app = sandbox.start_app(case.base_dir, case)
    try:
        with tempfile.TemporaryDirectory(prefix="cachai-rojo-") as tmp:
            inv = Path(tmp) / "inventory.json"
            inv.write_text(json.dumps(case.inventory))
            found = [Attack.from_dict(d) for d in sandbox.run_redteam(app, case, inv, args.budget)]
    finally:
        sandbox.stop(app)
    text = json.dumps([a.to_dict() for a in found], ensure_ascii=False, indent=2) + "\n"
    if args.write:
        (case.root / (case.attacks_file or "attacks.json")).write_text(text)
        groups = sorted({a.group_key for a in found})
        print(f"{len(found)} ataques confirmados contra la base, en {len(groups)} grupos")
    else:
        print(text, end="")
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    case = load_case(args.case)
    rows = fixer.run(case, args.agent, args.runs, args.model, args.out, args.budget, policy=args.policy, isolate=not args.no_isolation)
    fixer.write_summary(case, rows, args.out)
    for r in rows:
        if r["error"] and not r["approved"]:
            print(f"{r['name']}: {r['error']}")
            continue
        a = r["approved"]
        print(f"{r['name']}: propia validación {'aprobada' if a['visible'] else 'RECHAZADA'}, Cachai {'APROBADO' if a['full'] else 'RECHAZADO'}")
        for reason in r["reasons"][:4]:
            print(f"  - {reason}")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    cases = bench.find_cases(args.cases)
    if not cases:
        print("no hay casos", file=sys.stderr)
        return 2
    rows = bench.run(cases, jobs=args.jobs, budget=args.budget)
    summary = bench.write(rows, cases, args.out)
    print(bench.to_markdown(rows, summary, cases, bench.load_bots(args.out)))
    return 0


def cmd_case(args: argparse.Namespace) -> int:
    case = load_case(args.case)
    prs = [p.name for p in case.prs] if args.pr == "all" else [case.pr(args.pr).name]
    modes = list(pipeline.MODES) if args.mode == "all" else [args.mode]
    if args.report and len(prs) * len(modes) != 1:
        print("--report guarda una sola corrida: elige un PR con --pr y un modo con --mode", file=sys.stderr)
        return 2
    results = run_case(case, prs, modes, args.budget)
    if args.json:
        if len(results) == 1:
            save_report(next(iter(results.values())), args)
        print(json.dumps({f"{pr}/{mode}": r.to_dict() for (pr, mode), r in results.items()}, ensure_ascii=False, indent=2))
    elif len(results) == 1:
        _print(next(iter(results.values())), args)
    else:
        color = use_color(args)

        def cell(r: pipeline.CheckResult) -> str:
            text = ("aprobado" if r.approved else "RECHAZADO").ljust(9)
            return f"\033[{'32' if r.approved else '1;31'}m{text}\033[0m" if color else text

        width = max(len(p) for p in prs)
        print(f"{'PR'.ljust(width)}  {'tipo'.ljust(14)}  " + "  ".join(m.ljust(9) for m in modes))
        for pr in prs:
            cells = "  ".join(cell(results[pr, m]) for m in modes)
            print(f"{pr.ljust(width)}  {case.pr(pr).kind.ljust(14)}  {cells}")
        for (pr, mode), r in results.items():
            if mode == "full":
                print(f"\n[{pr}] {r.to_text(color).lstrip()}")
    return 0 if all(r.approved for r in results.values()) else 1


def cmd_hidden(args: argparse.Namespace) -> int:
    """Paquete reservado: `keygen` crea la clave, `seal` lo cifra y `open` lo extrae."""
    if args.action == "keygen":
        print(hidden.keygen())
        return 0
    key = os.environ.get(args.key_env)
    try:
        if args.action == "seal":
            if not args.out:
                print("seal necesita --out", file=sys.stderr)
                return 2
            sealed = hidden.seal(key, Path(args.attacks) if args.attacks else None,
                                 Path(args.holdout_tests) if args.holdout_tests else None, args.shown, args.seed)
            Path(args.out).write_bytes(sealed)
            print(f"paquete reservado escrito en {args.out}")
            return 0
        if not args.package or not args.out:
            print("open necesita el paquete y --out", file=sys.stderr)
            return 2
        m = hidden.open_to_dir(Path(args.package).read_bytes(), key, Path(args.out))
        n_tests = sum(1 for _ in m.holdout_tests.rglob("test_*.py")) if m.holdout_tests else 0
        print(f"{len(m.attacks)} ataques ({len(m.split.reserved) if m.split else 0} reservados) y {n_tests} archivos de tests en {args.out}")
        return 0
    except hidden.HiddenError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def cmd_init(args: argparse.Namespace) -> int:
    families = None if args.families is None else [f for f in args.families.split(",") if f]
    opts = init.InitOptions(
        repo=Path(args.repo), yes=args.yes, force=args.force, module=args.module, port=args.port, health=args.health,
        tests_dir=args.tests_dir, users=args.user, logins=args.login, families=families,
        holdout_tests=Path(args.holdout_tests) if args.holdout_tests else None, package=args.package,
        key_env=args.key_env, key_file=Path(args.key_file) if args.key_file else None, shown=args.shown, seed=args.seed,
        budget=args.budget, sandbox=not args.no_sandbox, redteam=not args.no_redteam, add_health=args.add_health,
        workflow=not args.no_workflow,
    )
    try:
        result = init.run(opts)
    except (init.InitError, ConfigError, hidden.HiddenError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\ninit cancelado: no se escribió nada más", file=sys.stderr)
        return 130
    print(f"Listo en {result.seconds:.0f} s. Prueba un PR con: cachai check --repo {args.repo} --base main --pr <rama>")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cachai")
    sub = parser.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="configura un repo FastAPI: .cachai.json, workflow y paquete reservado",
                       description="Detecta la app, la levanta en el sandbox, propone el inventario, corre el equipo rojo "
                                   "contra la base y sella el paquete reservado. Escribe .cachai.json y "
                                   ".github/workflows/cachai.yml. Debe correrlo quien mantiene el repo: ve el material reservado.")
    i.add_argument("--repo", default=".")
    i.add_argument("--yes", "-y", action="store_true", help="no pregunta nada: acepta cada propuesta (agentes y CI)")
    i.add_argument("--force", action="store_true", help="reemplaza .cachai.json y el workflow si ya existen")
    i.add_argument("--module", help="objeto ASGI (paquete.modulo:app); por defecto, el detectado")
    i.add_argument("--port", type=int)
    i.add_argument("--health", help="ruta de salud; por defecto, la detectada")
    i.add_argument("--add-health", action="store_true", help="si no hay ruta de salud, agrega GET /health al módulo de la app")
    i.add_argument("--tests-dir")
    i.add_argument("--user", action="append", default=[], metavar="NOMBRE='Header: valor'",
                   help="usuario de prueba y un header suyo; se repite (al menos dos usuarios para el equipo rojo)")
    i.add_argument("--login", action="append", default=[], metavar="NOMBRE='comando'",
                   help="comando de login de un usuario; su última línea es «Header: valor» o un token Bearer")
    i.add_argument("--families", help="familias de ataque separadas por coma; por defecto, según el inventario")
    i.add_argument("--holdout-tests", help="carpeta con tests funcionales reservados para sellar (fuera del repo)")
    i.add_argument("--package", default=init.DEFAULT_PACKAGE, help="dónde escribir el paquete reservado cifrado")
    i.add_argument("--key-env", default=init.DEFAULT_KEY_ENV, help="secreto del CI con la clave (si existe en el entorno, se usa)")
    i.add_argument("--key-file", help="escribe la clave nueva en este archivo (0600) en vez de imprimirla")
    i.add_argument("--shown", type=float, default=0.4, help="proporción de grupos de ataques visibles")
    i.add_argument("--seed", type=int, default=0)
    i.add_argument("--budget", type=float, default=60.0, help="segundos para leer la app y para el equipo rojo")
    i.add_argument("--no-sandbox", action="store_true", help="solo detecta y escribe: sin inventario ni equipo rojo")
    i.add_argument("--no-redteam", action="store_true")
    i.add_argument("--no-workflow", action="store_true")
    i.set_defaults(func=cmd_init)

    c = sub.add_parser("check", help="revisa un PR escrito por IA antes del merge")
    c.add_argument("--repo", default=".")
    c.add_argument("--base", default="main")
    c.add_argument("--pr", required=True)
    c.add_argument("--mode", choices=pipeline.MODES, default=None,
                   help="por defecto: full si hay material reservado (--holdout-tests); si no, visible y lo avisa")
    c.add_argument("--holdout-tests", help="carpeta con tests funcionales reservados, fuera del repo")
    c.add_argument("--attacks", help="JSON con los ataques que el parche debe bloquear, fuera del repo")
    c.add_argument("--shown", type=float, default=0.4, help="proporción de grupos de ataques que vio el parchador")
    c.add_argument("--seed", type=int, default=0, help="semilla de la selección visible/reservada")
    c.add_argument("--public", action="store_true",
                   help="imprime solo la versión pública del reporte (para CI: el autor del PR ve el log)")
    c.set_defaults(func=cmd_check)

    k = sub.add_parser("case", help="revisa los PR de un caso de evaluación")
    k.add_argument("case")
    k.add_argument("--pr", default="all")
    k.add_argument("--mode", choices=(*pipeline.MODES, "all"), default="all")
    k.set_defaults(func=cmd_case)

    r = sub.add_parser("redteam", help="genera y confirma las variantes de ataque de un caso")
    r.add_argument("case")
    r.add_argument("--write", action="store_true", help="guarda el resultado en el archivo de ataques del caso")
    r.add_argument("--budget", type=float, default=60.0)
    r.set_defaults(func=cmd_redteam)

    b = sub.add_parser("bench", help="corre todos los PR etiquetados en cada modo y resume")
    b.add_argument("cases", nargs="?", default="cases")
    b.add_argument("--out", default="results", help="carpeta donde se escriben bench.json, resultados.md y los reportes")
    b.add_argument("--jobs", type=int, default=4)
    b.add_argument("--budget", type=float, default=120.0)
    b.set_defaults(func=cmd_bench)

    f = sub.add_parser("fix", help="pide parches a un agente de código real y los revisa")
    f.add_argument("case")
    f.add_argument("--agent", choices=sorted(fixer.AGENTS), default="claude")
    f.add_argument("--model", help="modelo del agente; por defecto, el habitual de cada uno")
    f.add_argument("--runs", type=int, default=3)
    f.add_argument("--policy", choices=sorted(fixer.POLICIES), default="normal",
                   help="normal: arreglar la falla; minimo: parchador instruido para tapar solo lo que vio")
    f.add_argument("--out", default="results/bots")
    f.add_argument("--no-isolation", action="store_true",
                   help="corre al agente sin aislarlo del material reservado; queda registrado en cada intento")
    f.add_argument("--budget", type=float, default=120.0)
    f.set_defaults(func=cmd_fix)

    h = sub.add_parser("hidden", help="cifra o abre el paquete de material reservado (lo usa quien mantiene el repo)")
    h.add_argument("action", choices=("keygen", "seal", "open"))
    h.add_argument("package", nargs="?", help="paquete a abrir (open)")
    h.add_argument("--attacks", help="seal: JSON de ataques")
    h.add_argument("--holdout-tests", help="seal: carpeta de tests funcionales reservados")
    h.add_argument("--shown", type=float, default=0.4, help="seal: proporción de grupos de ataques visibles")
    h.add_argument("--seed", type=int, default=0)
    h.add_argument("--out", help="seal: archivo de salida; open: carpeta nueva")
    h.add_argument("--key-env", default="CACHAI_HIDDEN_KEY", help="variable de entorno con la clave")
    h.set_defaults(func=cmd_hidden)

    for p in (c, k):
        p.add_argument("--budget", type=float, default=120.0, help="segundos por evaluación")
        p.add_argument("--json", action="store_true")
        p.add_argument("--color", choices=("auto", "always", "never"), default="auto")
        p.add_argument("--report", help="guarda el reporte público v2 (schema/report.schema.json) en este archivo (una sola corrida)")

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
