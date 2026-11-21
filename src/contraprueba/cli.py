"""CLI: `contraprueba check --repo <ruta> --base main --pr <rama>`."""

from __future__ import annotations

import argparse
import sys

from . import integrity
from .gate import Evidence, decide


def check(repo: str, base: str, pr: str) -> int:
    flags = integrity.classify(integrity.changed_files(repo, base, pr))
    # Pendiente: levantar app y evaluador en contenedores separados y correr
    # los tests de la base y los ataques y tests reservados (sandbox, holdout).
    ev = Evidence(base_tests_passed=True, integrity_flags=flags)
    verdict = decide(ev)
    print("APROBADO" if verdict.approved else "RECHAZADO")
    for r in verdict.reasons:
        print(f"  - {r}")
    return 0 if verdict.approved else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="contraprueba")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="revisa un PR escrito por IA antes del merge")
    c.add_argument("--repo", default=".")
    c.add_argument("--base", default="main")
    c.add_argument("--pr", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "check":
        return check(args.repo, args.base, args.pr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
