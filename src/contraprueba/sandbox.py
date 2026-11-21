"""App y evaluador en contenedores separados.

La app del PR corre en un contenedor; el evaluador, con los tests de la rama
base, corre en otro y le hace pedidos por HTTP. El código del PR nunca comparte
proceso ni contenedor con quien lo califica. Cada corrida tiene su propia red
Docker interna, sin salida a internet.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .case import Case, Target
from .integrity import EVAL_CONFIG, is_hook

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


def _copy_regular_tree(src: Path, dst: Path) -> None:
    """Copia un árbol de archivos regulares. No sigue ni copia enlaces simbólicos."""
    if src.is_symlink() or not src.is_dir():
        raise SandboxError(f"se esperaba una carpeta real: {src}")
    for root, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in dirs + files:
            if (Path(root) / name).is_symlink():
                raise SandboxError(f"enlace simbólico en los tests de la base: {Path(root) / name}")
        for name in files:
            f = Path(root) / name
            target = dst / f.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, target)


def _plain_path(root: Path, rel: str) -> Path:
    """`root/rel` si ningún componente es un enlace simbólico; si no, error."""
    path = root
    for part in Path(rel).parts:
        if part in ("..", "/") or Path(part).is_absolute():
            raise SandboxError(f"ruta fuera del árbol: {rel}")
        path = path / part
        if path.is_symlink():
            raise SandboxError(f"enlace simbólico en la ruta: {path}")
    return path


def _claim_path(root: Path, rel: str | Path) -> Path:
    """Devuelve `root/rel` garantizando que sus carpetas ancestro sean reales.

    Cualquier ancestro que sea un enlace o un archivo se reemplaza por una
    carpeta vacía, sin seguirlo. La ruta final se deja como esté.
    """
    parts = Path(rel).parts
    if not parts or any(p in ("..", "/") or Path(p).is_absolute() for p in parts):
        raise SandboxError(f"ruta fuera del árbol: {rel}")
    path = root
    for part in parts[:-1]:
        path = path / part
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            path.unlink()
        path.mkdir(exist_ok=True)
    return path / parts[-1]


def prepare_eval_workdir(base_src: Path, tests_dir: str, dest: Path) -> Path:
    """Arma el directorio que ve el evaluador: tests y configuración de pytest de la base.

    `base_src` debe venir de la rama base, nunca del checkout del PR.
    """
    dest.mkdir(parents=True, exist_ok=True)
    _copy_regular_tree(_plain_path(base_src, tests_dir), dest / tests_dir)
    for name in EVAL_CONFIG:
        f = base_src / name
        if f.is_file() and not f.is_symlink():
            shutil.copyfile(f, dest / name)
    return dest


def _remove(path: Path) -> None:
    """Borra un archivo, enlace o carpeta sin seguir enlaces."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def restore_eval_files(pr_tree: Path, base_tree: Path, tests_dir: str) -> Path:
    """Línea base «restaurar»: deja en el árbol del PR los tests, la configuración
    y los hooks de la rama base. Modifica `pr_tree` y lo devuelve.

    Es la defensa habitual, reforzada: además de reponer los tests borra todo
    hook de evaluación (`conftest.py`, `sitecustomize.py`, `*.pth`) que no esté
    en la base. El código del PR sigue en el mismo contenedor que pytest.

    El árbol del PR no es confiable: nunca se borra ni se escribe a través de
    un enlace simbólico, tampoco si está en una carpeta ancestro.
    """
    for root, dirs, files in os.walk(pr_tree, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for name in files:
            if is_hook(name):
                _remove(Path(root) / name)
    for name in EVAL_CONFIG:
        _remove(pr_tree / name)
    tests_dst = _claim_path(pr_tree, tests_dir)
    _remove(tests_dst)
    for root, dirs, files in os.walk(base_tree, followlinks=False):
        for name in files:
            src = Path(root) / name
            rel = src.relative_to(base_tree)
            if src.is_symlink() or not (is_hook(name) or (len(rel.parts) == 1 and name in EVAL_CONFIG)):
                continue
            dst = _claim_path(pr_tree, rel)
            _remove(dst)
            shutil.copyfile(src, dst)
    _copy_regular_tree(_plain_path(base_tree, tests_dir), _claim_path(pr_tree, tests_dir))
    return pr_tree


def run_pytest_inside(context: Path, tests_dir: str, budget: float = 120.0) -> dict:
    """Líneas base: corre pytest dentro del mismo contenedor que el código.

    Devuelve lo que vería un CI: `passed` es verdadero si pytest salió con 0.
    """
    run_id = f"cp-{uuid.uuid4().hex[:10]}"
    image = f"{run_id}-ci"
    _docker("build", "-q", "-f", str(ROOT / "docker" / "app.Dockerfile"), "-t", image, str(context))
    try:
        try:
            proc = _docker(
                "run", "--rm", "--name", run_id, "--network", "none", *HARDENING,
                image, "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", tests_dir,
                timeout=budget, check=False,
            )
        except subprocess.TimeoutExpired:
            _docker("rm", "-f", run_id, check=False, timeout=60)
            return {"passed": False, "exit_code": None, "timed_out": True, "log_tail": ""}
        return {
            "passed": proc.returncode == 0,
            "exit_code": proc.returncode,
            "timed_out": False,
            "log_tail": (proc.stdout + proc.stderr)[-2000:],
        }
    finally:
        _docker("rmi", "-f", image, check=False, timeout=60)


def start_app(context: Path, case: Case | Target) -> AppHandle:
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


def run_evaluator(app: AppHandle, case: Case | Target, workdir: Path, holdout_dir: Path | None = None, budget: float = 120.0,
                  attacks_file: Path | None = None) -> tuple[int, dict]:
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
    if attacks_file is not None:
        args += ["-v", f"{attacks_file.resolve()}:/work/attacks.json:ro"]
    args += [
        EVALUATOR_IMAGE, "--base-url", app.base_url, "--health", case.app.health,
        "--tests-dir", case.tests_dir, "--budget", str(budget),
    ]
    if holdout_dir is not None:
        args += ["--holdout-dir", "/work/holdout"]
    if attacks_file is not None:
        args += ["--attacks", "/work/attacks.json"]
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
