"""Aceptación de `cachai init` (#7): init + check dan el mismo veredicto que `cachai case`.

    PYTHONPATH=$PWD/src python scripts/init_acceptance.py [cases/facturas] [--pr nombre ...]

1. Copia `base/` del caso a una carpeta limpia (sin `.cachai.json`, sin git).
2. Corre `cachai init --yes` ahí. Lo único que se le da: los headers de los
   usuarios de prueba (los tokens fijos del caso) y la carpeta de tests
   reservados del caso. El inventario, las familias y los ataques los propone init.
3. Arma el repo con `main` = esa carpeta tal como la dejó init (con su
   `.cachai.json`, el paquete sellado y el workflow) y una rama por PR, aplicando
   las capas del caso (`materialize(..., base=...)`).
4. Corre `cachai check --mode full` por PR con la clave en el entorno, y
   `cachai case --mode full` sobre el caso original, y compara.

Mide el tiempo de cada paso. Sale con 0 si todos los veredictos coinciden.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cachai import hidden
from cachai.case import load_case, materialize


def cachai(*args: str, env: dict | None = None) -> tuple[subprocess.CompletedProcess, float]:
    t = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "cachai.cli", *args], capture_output=True, text=True, env=env)
    return proc, time.monotonic() - t


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("case", nargs="?", default="cases/facturas")
    ap.add_argument("--pr", action="append", help="solo estos PR (por defecto, todos)")
    ap.add_argument("--json", help="guarda el resultado en este archivo")
    args = ap.parse_args(argv)
    case = load_case(args.case)
    prs = args.pr or [p.name for p in case.prs]
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "GITHUB_ACTIONS")}
    env["CACHAI_HIDDEN_KEY"] = hidden.keygen()
    users = [f"--user={name}={k}: {v}" for name, headers in case.inventory["users"].items() for k, v in headers.items()]

    with tempfile.TemporaryDirectory(prefix="cachai-aceptacion-") as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(case.base_dir, base, ignore=shutil.ignore_patterns("__pycache__"))
        init_args = ["init", "--repo", str(base), "--yes", *users]
        if case.holdout_tests_path:
            init_args += ["--holdout-tests", str(case.holdout_tests_path)]
        proc, t_init = cachai(*init_args, env=env)
        if proc.returncode != 0:
            print(proc.stdout + proc.stderr, file=sys.stderr)
            return 2
        print(f"init: {t_init:.1f} s")
        repo = materialize(case, Path(tmp) / "repo", base=base)

        t0 = time.monotonic()
        checks: dict[str, dict] = {}
        for pr in prs:
            proc, t = cachai("check", "--repo", str(repo), "--base", "main", "--pr", pr, "--mode", "full", "--json", env=env)
            try:
                r = json.loads(proc.stdout)
            except json.JSONDecodeError:
                r = {"outcome": "error", "reasons": [proc.stderr.strip()[-300:]], "notes": []}
            checks[pr] = {"outcome": r.get("outcome"), "seconds": round(t, 1), "reasons": r.get("reasons", []), "notes": r.get("notes", [])}
            print(f"check {pr}: {checks[pr]['outcome']} ({t:.1f} s)", flush=True)
        t_checks = time.monotonic() - t0

    case_args = ["case", args.case, "--mode", "full", "--json"] + (["--pr", prs[0]] if len(prs) == 1 else [])
    proc, t_case = cachai(*case_args, env=env)
    ref = {k.split("/")[0]: v for k, v in json.loads(proc.stdout).items()}

    rows, same = [], True
    for pr in prs:
        a, b = ref[pr]["outcome"], checks[pr]["outcome"]
        same &= a == b
        rows.append({"pr": pr, "kind": case.pr(pr).kind, "case": a, "init_check": b, "same": a == b,
                     "seconds": checks[pr]["seconds"], "reasons": checks[pr]["reasons"], "notes": checks[pr]["notes"]})
    print(f"\n| PR | tipo | cachai case | init + check | igual | s |\n|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['pr']} | {r['kind']} | {r['case']} | {r['init_check']} | {'sí' if r['same'] else '**NO**'} | {r['seconds']} |")
    print(f"\ninit {t_init:.1f} s · {len(prs)} checks {t_checks:.1f} s · punta a punta {t_init + t_checks:.1f} s"
          f" · cachai case (referencia) {t_case:.1f} s")
    if args.json:
        Path(args.json).write_text(json.dumps({"rows": rows, "init_s": t_init, "checks_s": t_checks, "case_s": t_case},
                                              ensure_ascii=False, indent=2) + "\n")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
