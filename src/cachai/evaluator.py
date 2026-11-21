"""Punto de entrada del contenedor evaluador.

Corre dentro de la imagen fija de Cachai, nunca junto al código del PR.
Espera a que la app responda, corre los tests de la rama base (y los reservados,
si hay) contra ella por HTTP y escribe en stdout un JSON con esquema versionado.

Contrato de salida (`schema` 1):

- `status`: `ok` (todo pasó), `failed` (la evaluación terminó y algo falló) o
  `incomplete` (algo no se pudo ejecutar: app caída, timeout, sin tests).
- `suites.<nombre>`: resultado por suite (`base`, `holdout`). `failures` trae
  archivo, línea y mensaje de cada test que falló.
- `attacks`: resultado por ataque (`confirmed`, `blocked` o `error`), o `null`
  si no se pidieron ataques.
- `errors`: errores de infraestructura.
- `log_tail` en cada suite es solo informativo; no es parte del contrato.

Código de salida: 0 = ok, 1 = failed, 2 = incomplete.

Política: una suite pasa solo si terminó dentro del presupuesto, corrió al
menos un test y no hubo fallos, errores ni tests omitidos (las suites del
evaluador son obligatorias; `skip` y `xfail` cuentan como no ejecutados).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from .holdout import CLIENT_HEADERS_ENV, browser_headers, load_attacks, run_attacks

SCHEMA = 1
EXIT = {"ok": 0, "failed": 1, "incomplete": 2}


def remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def wait_for(url: str, deadline: float) -> bool:
    """Espera a que `url` responda, sin pasarse del plazo: una respuesta tardía no cuenta."""
    while (left := remaining(deadline)) > 0:
        try:
            with urllib.request.urlopen(url, timeout=min(2.0, left)) as r:
                if r.status < 500 and remaining(deadline) > 0:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(min(0.5, max(remaining(deadline), 0)))
    return False


def parse_junit(path: Path) -> list[dict]:
    """Devuelve un resultado por test: nodeid, outcome (passed, failed, error, skipped),
    archivo, línea (desde 1) y, si falló, la primera línea del mensaje."""
    results = []
    for tc in ET.parse(path).getroot().iter("testcase"):
        nodeid = f"{tc.get('classname', '')}::{tc.get('name', '')}"
        outcome, message = "passed", ""
        for child in tc:
            if child.tag in ("failure", "error", "skipped"):
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[child.tag]
                message = (child.get("message") or "").strip().splitlines()[0][:200] if child.get("message") else ""
                if outcome == "skipped" and NOT_EXTERNAL in (child.text or ""):
                    # Salto en la colección: el motivo va en el texto, no en el atributo.
                    text = child.text or ""
                    message = text[text.index(NOT_EXTERNAL):].rstrip("')\"")[:200]
                break
        line = tc.get("line")
        results.append({
            "nodeid": nodeid,
            "outcome": outcome,
            "file": tc.get("file"),
            "line": int(line) + 1 if line and line.isdigit() else None,
            "message": message,
        })
    return results


NOT_EXTERNAL = "cachai:no-externo"  # lo pone `pytest_http` a los tests que usan el código de la app


def summarize(cases: list[dict], exit_code: int | None, timed_out: bool, log: str) -> dict:
    failed = [c["nodeid"] for c in cases if c["outcome"] in ("failed", "error")]
    outside = [c for c in cases if c["outcome"] == "skipped" and NOT_EXTERNAL in (c.get("message") or "")]
    skipped = [c["nodeid"] for c in cases if c["outcome"] == "skipped" and c not in outside]
    passed = sum(c["outcome"] == "passed" for c in cases)
    complete = not timed_out and exit_code is not None and passed + len(failed) > 0
    return {
        "complete": complete,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "total": len(cases),
        "passed": passed,
        "failed": failed,
        "failures": [
            {k: c.get(k) for k in ("nodeid", "file", "line", "message")}
            for c in cases if c["outcome"] in ("failed", "error")
        ],
        "skipped": skipped,
        # Tests que no se pueden evaluar desde afuera: no aprueban ni fallan, se informan.
        "not_external": [{"nodeid": c["nodeid"], "reason": (c.get("message") or "").split(": ", 1)[-1]} for c in outside],
        "ok": complete and exit_code == 0 and not failed and not skipped,
        "log_tail": log[-2000:],
    }


def run_suite(base_url: str, rootdir: Path, target: str, budget: float, client_headers: dict[str, str] | None = None) -> dict:
    if budget <= 0:
        return summarize([], None, True, "presupuesto agotado antes de empezar")
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        env = {
            **os.environ,
            "BASE_URL": base_url,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if client_headers:
            # El plugin cachai.pytest_http las pone por defecto en todo cliente httpx de los tests (#18).
            env[CLIENT_HEADERS_ENV] = json.dumps(client_headers)
        cmd = [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "cachai.pytest_http", "-o", "junit_family=xunit1",
            f"--junitxml={junit}", f"--rootdir={rootdir}", str(rootdir / target),
        ]
        try:
            proc = subprocess.run(cmd, cwd=rootdir, env=env, capture_output=True, text=True, timeout=budget)
            exit_code, timed_out, log = proc.returncode, False, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as e:
            out = e.stdout or ""
            exit_code, timed_out, log = None, True, out if isinstance(out, str) else out.decode(errors="replace")
        cases = parse_junit(junit) if junit.exists() else []
    return summarize(cases, exit_code, timed_out, log)


def overall_status(result: dict) -> str:
    if result["errors"]:
        return "incomplete"
    suites = [s for s in result["suites"].values() if s is not None]
    attacks = result.get("attacks")
    if not suites or any(not s["complete"] for s in suites) or (attacks and not attacks["complete"]):
        return "incomplete"
    if all(s["ok"] for s in suites) and not (attacks and attacks["confirmed"]):
        return "ok"
    return "failed"


def evaluate(
    base_url: str, health: str, base_dir: Path, tests_dir: str, holdout_dir: Path | None, budget: float,
    attacks_file: Path | None = None, run_base: bool = True,
) -> dict:
    """Toda la evaluación (salud, suites) cabe en `budget` segundos o queda incompleta."""
    if not (isinstance(budget, (int, float)) and math.isfinite(budget) and budget > 0):
        raise ValueError(f"presupuesto inválido: {budget!r}")
    deadline = time.monotonic() + budget
    # Un solo perfil de navegador por corrida para tests y ataques: la app no puede
    # distinguir unos de otros, ni a ninguno de un usuario real (#18).
    headers = browser_headers()
    result: dict = {"schema": SCHEMA, "base_url": base_url, "errors": [], "suites": {"base": None, "holdout": None}, "attacks": None}
    if not wait_for(base_url.rstrip("/") + health, deadline):
        result["errors"].append("la app no respondió en la ruta de salud dentro del plazo")
    else:
        result["suites"]["base"] = run_suite(base_url, base_dir, tests_dir, remaining(deadline), headers)
        if holdout_dir is not None:
            result["suites"]["holdout"] = run_suite(base_url, holdout_dir, ".", remaining(deadline), headers)
        if attacks_file is not None:
            result["attacks"] = run_attacks(base_url, load_attacks(attacks_file), deadline, headers=headers).to_dict()
    if remaining(deadline) < 0:
        result["errors"].append("la evaluación terminó fuera de plazo")
    result["status"] = overall_status(result)
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cachai-evaluator")
    p.add_argument("--base-url", required=True)
    p.add_argument("--health", default="/health")
    p.add_argument("--base-dir", default="/work/base", help="tests y configuración de la rama base")
    p.add_argument("--tests-dir", default="tests")
    p.add_argument("--holdout-dir", default=None, help="tests funcionales reservados")
    p.add_argument("--attacks", default=None, help="archivo JSON con los ataques que la app debe bloquear")
    p.add_argument("--budget", type=float, default=120.0, help="segundos para toda la evaluación")
    p.add_argument("--skip-base", action="store_true", help="los tests de la base los corre otro runner (jest)")
    args = p.parse_args(argv)
    result = evaluate(
        args.base_url, args.health, Path(args.base_dir), args.tests_dir,
        Path(args.holdout_dir) if args.holdout_dir else None, args.budget,
        Path(args.attacks) if args.attacks else None, not args.skip_base,
    )
    print(json.dumps(result, ensure_ascii=False))
    return EXIT[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
