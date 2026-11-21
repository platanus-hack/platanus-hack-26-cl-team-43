"""Comprobación de la fase 0.

Construye ambas imágenes, levanta la app de la rama base del caso en una red
interna sin internet y corre los tests de la base desde el contenedor
evaluador, con los valores de `case.json`. Imprime el JSON del evaluador y sale
con su código: 0 ok, 1 failed, 2 incomplete.

Uso: python scripts/smoke_fase0.py [carpeta del caso] [--budget segundos]
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

from contraprueba import sandbox
from contraprueba.case import load_case

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("case", nargs="?", default=str(ROOT / "cases" / "facturas"))
    p.add_argument("--budget", type=float, default=120.0)
    args = p.parse_args()
    case = load_case(args.case)
    sandbox.build_evaluator_image()
    app = sandbox.start_app(case.base_dir, case)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = sandbox.prepare_eval_workdir(case.base_dir, case.tests_dir, Path(tmp) / "base")
            code, result = sandbox.run_evaluator(app, case, workdir, case.holdout_tests_path, args.budget)
    finally:
        sandbox.stop(app)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
