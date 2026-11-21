"""CLI de Contraprueba.

    contraprueba check --repo <ruta> --base main --pr <rama> [--mode ci|restore|visible|full]
    contraprueba case <carpeta del caso> [--pr <nombre>|all] [--mode <modo>|all]
    contraprueba redteam <carpeta del caso> [--write]

`check` revisa un PR de un repo cuya rama base tiene `.contraprueba.json`. Si
el PR es un parche de seguridad, `--attacks` son los ataques que debe bloquear.
`case` arma el repo de un caso de `cases/` y lo revisa; con `all` imprime la
tabla de modos por PR. `redteam` genera las variantes de ataque del caso y las
confirma contra su rama base. `check` y `case` salen con 0 si todo se aprueba.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import pipeline, sandbox
from .case import load_case, materialize
from .holdout import Attack, load_attacks, split_record


def _print(result: pipeline.CheckResult, as_json: bool) -> None:
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) if as_json else result.to_text())


def cmd_check(args: argparse.Namespace) -> int:
    holdout = Path(args.holdout_tests) if args.holdout_tests else None
    plan = None
    if args.attacks:
        attacks = load_attacks(args.attacks)
        plan = pipeline.AttackPlan(tuple(attacks), split_record(attacks, args.shown, args.seed))
    result = pipeline.check(args.repo, args.base, args.pr, args.mode, holdout_tests=holdout, budget=args.budget, plan=plan)
    _print(result, args.json)
    return 0 if result.approved else 1


def run_case(case, prs: list[str], modes: list[str], budget: float = 120.0) -> dict[tuple[str, str], pipeline.CheckResult]:
    """Revisa los PR de un caso en los modos pedidos. Los parches de seguridad
    reciben además los ataques del caso, con su selección visible/reservada."""
    attacks, split = case.attacks(), case.split()
    plan = pipeline.AttackPlan(tuple(attacks), split) if split else None
    results: dict[tuple[str, str], pipeline.CheckResult] = {}
    with tempfile.TemporaryDirectory(prefix="contraprueba-caso-") as tmp:
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
        with tempfile.TemporaryDirectory(prefix="contraprueba-rojo-") as tmp:
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


def cmd_case(args: argparse.Namespace) -> int:
    case = load_case(args.case)
    prs = [p.name for p in case.prs] if args.pr == "all" else [case.pr(args.pr).name]
    modes = list(pipeline.MODES) if args.mode == "all" else [args.mode]
    results = run_case(case, prs, modes, args.budget)
    if args.json:
        print(json.dumps({f"{pr}/{mode}": r.to_dict() for (pr, mode), r in results.items()}, ensure_ascii=False, indent=2))
    elif len(results) == 1:
        print(next(iter(results.values())).to_text())
    else:
        width = max(len(p) for p in prs)
        print(f"{'PR'.ljust(width)}  {'tipo'.ljust(14)}  " + "  ".join(m.ljust(9) for m in modes))
        for pr in prs:
            cells = "  ".join(("aprobado" if results[pr, m].approved else "RECHAZADO").ljust(9) for m in modes)
            print(f"{pr.ljust(width)}  {case.pr(pr).kind.ljust(14)}  {cells}")
        for (pr, mode), r in results.items():
            if mode == "full":
                print(f"\n[{pr}] {r.to_text()}")
    return 0 if all(r.approved for r in results.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="contraprueba")
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="revisa un PR escrito por IA antes del merge")
    c.add_argument("--repo", default=".")
    c.add_argument("--base", default="main")
    c.add_argument("--pr", required=True)
    c.add_argument("--mode", choices=pipeline.MODES, default="full")
    c.add_argument("--holdout-tests", help="carpeta con tests funcionales reservados, fuera del repo")
    c.add_argument("--attacks", help="JSON con los ataques que el parche debe bloquear, fuera del repo")
    c.add_argument("--shown", type=float, default=0.4, help="proporción de grupos de ataques que vio el parchador")
    c.add_argument("--seed", type=int, default=0, help="semilla de la selección visible/reservada")
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

    for p in (c, k):
        p.add_argument("--budget", type=float, default=120.0, help="segundos por evaluación")
        p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
