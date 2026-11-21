"""App y evaluador en contenedores separados.

La app del PR corre en un contenedor; el evaluador, con los tests de la rama
base, corre en otro y le hace pedidos por HTTP. El código del PR nunca comparte
proceso ni contenedor con quien lo califica. Cada corrida tiene su propia red
Docker interna, sin salida a internet.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .case import Case
from .integrity import EVAL_CONFIG

ROOT = Path(__file__).resolve().parents[2]
EVALUATOR_IMAGE = "contraprueba-evaluator"
HARDENING = [
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--memory", "512m",
    "--pids-limit", "256",
    "--read-only",
    "--tmpfs", "/tmp",
]


class SandboxError(RuntimeError):
    pass


def _docker(*args: str, timeout: float | None = 600, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise SandboxError(f"docker {args[0]} falló: {proc.stderr.strip()[-500:]}")
    return proc


@dataclass
class AppHandle:
    run_id: str
    network: str
    container: str
    image: str
    base_url: str


def build_evaluator_image(root: Path = ROOT) -> str:
    _docker("build", "-q", "-f", str(root / "docker" / "evaluator.Dockerfile"), "-t", EVALUATOR_IMAGE, str(root))
    return EVALUATOR_IMAGE


def prepare_eval_workdir(base_src: Path, tests_dir: str, dest: Path) -> Path:
    """Arma el directorio que ve el evaluador: tests y configuración de pytest de la base.

    `base_src` debe venir de la rama base, nunca del checkout del PR.
    """
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(base_src / tests_dir, dest / tests_dir, symlinks=False, ignore=shutil.ignore_patterns("__pycache__"))
    for name in EVAL_CONFIG:
        f = base_src / name
        if f.is_file() and not f.is_symlink():
            shutil.copyfile(f, dest / name)
    return dest


def start_app(context: Path, case: Case) -> AppHandle:
    """Construye y levanta la app de `context` en un contenedor aislado, sin salida a internet."""
    run_id = f"cp-{uuid.uuid4().hex[:10]}"
    image = f"{run_id}-app"
    _docker("build", "-q", "-f", str(ROOT / "docker" / "app.Dockerfile"), "-t", image, str(context))
    _docker("network", "create", "--internal", run_id)
    container = f"{run_id}-app"
    try:
        _docker(
            "run", "-d", "--name", container, "--network", run_id, "--network-alias", "app", *HARDENING,
            image, "uvicorn", case.app.module, "--host", "0.0.0.0", "--port", str(case.app.port),
        )
    except SandboxError:
        stop(AppHandle(run_id, run_id, container, image, ""))
        raise
    return AppHandle(run_id, run_id, container, image, f"http://app:{case.app.port}")


def run_evaluator(app: AppHandle, case: Case, workdir: Path, holdout_dir: Path | None = None, budget: float = 120.0) -> tuple[int, dict]:
    """Corre el evaluador contra la app. Devuelve (código de salida, JSON del evaluador).

    Si el contenedor no termina en `budget` + margen, se mata y se devuelve una
    evaluación incompleta.
    """
    name = f"{app.run_id}-eval"
    args = [
        "run", "--rm", "--name", name, "--network", app.network, *HARDENING,
        "-v", f"{workdir.resolve()}:/work/base:ro",
    ]
    if holdout_dir is not None:
        args += ["-v", f"{holdout_dir.resolve()}:/work/holdout:ro"]
    args += [
        EVALUATOR_IMAGE, "--base-url", app.base_url, "--health", case.app.health,
        "--tests-dir", case.tests_dir, "--budget", str(budget),
    ]
    if holdout_dir is not None:
        args += ["--holdout-dir", "/work/holdout"]
    try:
        proc = _docker(*args, timeout=budget + 45, check=False)
    except subprocess.TimeoutExpired:
        _docker("rm", "-f", name, check=False, timeout=60)
        return 2, {"schema": 1, "status": "incomplete", "errors": ["el evaluador no terminó a tiempo"], "suites": {}, "attacks": None}
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return 2, {"schema": 1, "status": "incomplete", "errors": [f"salida ilegible del evaluador: {proc.stderr.strip()[-500:]}"], "suites": {}, "attacks": None}
    return proc.returncode, result


def stop(app: AppHandle) -> None:
    _docker("rm", "-f", app.container, check=False, timeout=60)
    _docker("network", "rm", app.network, check=False, timeout=60)
    _docker("rmi", "-f", app.image, check=False, timeout=60)
