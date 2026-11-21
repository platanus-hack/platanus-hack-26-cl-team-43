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
import threading
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


# Dos revisiones del mismo árbol producen la misma imagen. Si una la borra justo
# cuando la otra termina de construirla, la segunda se queda sin imagen: construir
# y borrar se hacen de a uno.
_image_lock = threading.Lock()
MISSING_IMAGE = ("Unable to find image", "No such image", "pull access denied")


def _build_app_image(context: Path, image: str) -> None:
    with _image_lock:
        _docker("build", "-q", "-f", str(ROOT / "docker" / "app.Dockerfile"), "-t", image, str(context))


def _remove_image(image: str) -> None:
    with _image_lock:
        _docker("rmi", "-f", image, check=False, timeout=60)


_built: set[str] = set()
_build_lock = threading.Lock()


def build_evaluator_image(root: Path = ROOT) -> str:
    """Construye la imagen del evaluador una vez por proceso."""
    with _build_lock:
        if str(root) not in _built:
            _docker("build", "-q", "-f", str(root / "docker" / "evaluator.Dockerfile"), "-t", EVALUATOR_IMAGE, str(root))
            _built.add(str(root))
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
    _build_app_image(context, image)
    cmd = [
        "run", "--rm", "--name", run_id, "--network", "none", *HARDENING,
        image, "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", tests_dir,
    ]
    try:
        try:
            proc = _docker(*cmd, timeout=budget, check=False)
            if proc.returncode == 125 and any(m in proc.stderr for m in MISSING_IMAGE):
                _build_app_image(context, image)  # otra corrida fuera de este proceso la borró
                proc = _docker(*cmd, timeout=budget, check=False)
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
        _remove_image(image)


def start_app(context: Path, case: Case | Target) -> AppHandle:
    """Construye y levanta la app de `context` en un contenedor aislado, sin salida a internet."""
    run_id = f"cp-{uuid.uuid4().hex[:10]}"
    image = f"{run_id}-app"
    _build_app_image(context, image)
    container = f"{run_id}-app"
    cmd = [
        "run", "-d", "--name", container, "--network", run_id, "--network-alias", "app", *HARDENING,
        image, "uvicorn", case.app.module, "--host", "0.0.0.0", "--port", str(case.app.port),
    ]
    try:
        _docker("network", "create", "--internal", run_id)
        try:
            _docker(*cmd)
        except SandboxError as e:
            if not any(m in str(e) for m in MISSING_IMAGE):
                raise
            _docker("rm", "-f", container, check=False, timeout=60)
            _build_app_image(context, image)  # otra corrida fuera de este proceso la borró
            _docker(*cmd)
    except SandboxError:
        stop(AppHandle(run_id, run_id, container, image, ""))
        raise
    return AppHandle(run_id, run_id, container, image, f"http://app:{case.app.port}")


INCOMPLETE = {"schema": 1, "status": "incomplete", "suites": {}, "attacks": None}


def _run_trusted(app: AppHandle, name: str, mounts: list[tuple[Path, str]], args: list[str], budget: float,
                 entrypoint: list[str] | None = None) -> tuple[int | None, str, str]:
    """Corre la imagen de Contraprueba en la red de la app. Devuelve (código, stdout, stderr);
    código None si hubo que matarlo por tiempo."""
    cmd = ["run", "--rm", "--name", name, "--network", app.network, *HARDENING]
    for src, dst in mounts:
        cmd += ["-v", f"{src.resolve()}:{dst}:ro"]
    if entrypoint:
        cmd += ["--entrypoint", entrypoint[0]]
    cmd += [EVALUATOR_IMAGE, *(entrypoint[1:] if entrypoint else []), *args]
    try:
        proc = _docker(*cmd, timeout=budget + 45, check=False)
    except subprocess.TimeoutExpired:
        _docker("rm", "-f", name, check=False, timeout=60)
        return None, "", ""
    return proc.returncode, proc.stdout, proc.stderr


def _last_json(stdout: str) -> dict | None:
    try:
        data = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def run_evaluator(app: AppHandle, case: Case | Target, workdir: Path, holdout_dir: Path | None = None,
                  budget: float = 120.0, attacks_file: Path | None = None) -> tuple[int, dict]:
    """Corre el evaluador contra la app. Devuelve (código de salida, JSON del evaluador).

    Si el contenedor no termina en `budget` + margen, se mata y se devuelve una
    evaluación incompleta.
    """
    mounts = [(workdir, "/work/base")]
    args = ["--base-url", app.base_url, "--health", case.app.health, "--tests-dir", case.tests_dir, "--budget", str(budget)]
    if holdout_dir is not None:
        mounts.append((holdout_dir, "/work/holdout"))
        args += ["--holdout-dir", "/work/holdout"]
    if attacks_file is not None:
        mounts.append((attacks_file, "/work/attacks.json"))
        args += ["--attacks", "/work/attacks.json"]
    code, out, err = _run_trusted(app, f"{app.run_id}-eval", mounts, args, budget)
    if code is None:
        return 2, {**INCOMPLETE, "errors": ["el evaluador no terminó a tiempo"]}
    result = _last_json(out)
    if result is None:
        return 2, {**INCOMPLETE, "errors": [f"salida ilegible del evaluador: {err.strip()[-500:]}"]}
    return code, result


def run_redteam(app: AppHandle, case: Case | Target, inventory_file: Path, budget: float = 60.0) -> list[dict]:
    """Genera y confirma variantes de ataque contra la app (que debe ser la de la rama base)."""
    code, out, err = _run_trusted(
        app, f"{app.run_id}-red", [(inventory_file, "/work/inventory.json")],
        ["--base-url", app.base_url, "--health", case.app.health, "--inventory", "/work/inventory.json", "--budget", str(budget)],
        budget, entrypoint=["python", "-m", "contraprueba.redteam"],
    )
    data = _last_json(out) if code == 0 else None
    if data is None or "attacks" not in data:
        raise SandboxError(f"el equipo rojo no terminó: {(out + err).strip()[-500:]}")
    return data["attacks"]


def stop(app: AppHandle) -> None:
    _docker("rm", "-f", app.container, check=False, timeout=60)
    _docker("network", "rm", app.network, check=False, timeout=60)
    _remove_image(app.image)
