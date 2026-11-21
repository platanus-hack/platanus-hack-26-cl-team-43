"""CLI de Contraprueba.

    contraprueba check --repo <ruta> --base main --pr <rama> [--mode ci|restore|full]
    contraprueba case <carpeta del caso> [--pr <nombre>|all] [--mode <modo>|all]

`check` revisa un PR de un repo cuya rama base tiene `.contraprueba.json`.
`case` arma el repo de un caso de `cases/` y lo revisa; con `all` imprime la
tabla de modos por PR. Sale con 0 si aprueba y 1 si rechaza.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import pipeline
from .case import load_case, materialize


def _print(result: pipeline.CheckResult, as_json: bool) -> None:
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) if as_json else result.to_text())


def cmd_check(args: argparse.Namespace) -> int:
    holdout = Path(args.holdout_tests) if args.holdout_tests else None
    result = pipeline.check(args.repo, args.base, args.pr, args.mode, holdout_tests=holdout, budget=args.budget)
    _print(result, args.json)
    return 0 if result.approved else 1


def cmd_case(args: argparse.Namespace) -> int:
    case = load_case(args.case)
    prs = [p.name for p in case.prs] if args.pr == "all" else [case.pr(args.pr).name]
    modes = list(pipeline.MODES) if args.mode == "all" else [args.mode]
    results: dict[tuple[str, str], pipeline.CheckResult] = {}
    with tempfile.TemporaryDirectory(prefix="contraprueba-caso-") as tmp:
        repo = materialize(case, Path(tmp) / "repo")
        for pr in prs:
            for mode in modes:
                results[pr, mode] = pipeline.check(
                    repo, "main", pr, mode, target=case.target(), holdout_tests=case.holdout_tests_path, budget=args.budget
                )
    if args.json:
        print(json.dumps({f"{pr}/{mode}": r.to_dict() for (pr, mode), r in results.items()}, ensure_ascii=False, indent=2))
    elif len(results) == 1:
        print(next(iter(results.values())).to_text())
    else:
        width = max(len(p) for p in prs)
        print(f"{'PR'.ljust(width)}  tipo           " + "  ".join(m.ljust(9) for m in modes))
        for pr in prs:
            cells = "  ".join(("aprobado" if results[pr, m].approved else "RECHAZADO").ljust(9) for m in modes)
            print(f"{pr.ljust(width)}  {case.pr(pr).kind.ljust(13)}  {cells}")
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
    c.set_defaults(func=cmd_check)

    k = sub.add_parser("case", help="revisa los PR de un caso de evaluación")
    k.add_argument("case")
    k.add_argument("--pr", default="all")
    k.add_argument("--mode", choices=(*pipeline.MODES, "all"), default="all")
    k.set_defaults(func=cmd_case)

    for p in (c, k):
        p.add_argument("--budget", type=float, default=120.0, help="segundos por evaluación")
        p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
