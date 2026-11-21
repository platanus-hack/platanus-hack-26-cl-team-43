"""Cosecha trazas verificadas para entrenar al equipo rojo y al parchador.

    python scripts/harvest_traces.py --out traces/ [--seed N] [--cases cases]

Corre `bench.run` en modo `full` (necesita Docker) y escribe JSONL con splits
train/val por caso. No llama a ningún modelo ni etiqueta nada: ver `cachai.bench.harvest`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from cachai import bench, llm, sandbox


def fetch_routes(case) -> list:
    """Plantillas de ruta GET de la app de la base, para el prompt rojo. Sin inventario no hay rojo."""
    if not case.inventory:
        return []
    app = sandbox.start_app(case.base_dir, case)
    try:
        with tempfile.TemporaryDirectory(prefix="cachai-rutas-") as tmp:
            inv = Path(tmp) / "inventory.json"
            inv.write_text(json.dumps(case.inventory))
            return sandbox.fetch_routes(app, case, inv)
    finally:
        sandbox.stop(app)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--cases", default="cases", help="carpeta de casos (o un caso)")
    p.add_argument("--out", default="traces", help="carpeta de salida (no se versiona)")
    p.add_argument("--seed", type=int, default=0, help="semilla del reparto train/val")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--budget", type=float, default=120.0)
    args = p.parse_args()
    cases = bench.find_cases(args.cases)
    if not cases:
        print("no hay casos", file=sys.stderr)
        return 2
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    rows = bench.run(cases, modes=("full",), jobs=args.jobs, budget=args.budget)
    sandbox.build_evaluator_image()
    routes = {c.id: fetch_routes(c) for c in cases}
    data = bench.harvest(cases, rows, commit, args.seed, routes)
    for path in bench.write_harvest(data, args.out):
        print(f"{path}: {len(data[path.stem])} ejemplos")
    if len(cases) < 2:
        llm.warn("con un solo caso no hay split val: todo queda en train")
    return 0


if __name__ == "__main__":
    sys.exit(main())
