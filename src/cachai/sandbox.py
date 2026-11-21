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
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import compose as compose_mod
from .case import Case, Target
from .integrity import EVAL_CONFIG, is_hook

ROOT = Path(__file__).resolve().parents[2]
EVALUATOR_IMAGE = "cachai-evaluator"
HARDENING = [
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--memory", "512m",
    "--pids-limit", "256",
    "--read-only",
    "--tmpfs", "/tmp",
]


# Servicios del compose de la base (base de datos, colas): imágenes que eligió la
# base, no el PR. Necesitan escribir y cambiar de usuario, así que no llevan
# `--read-only` ni `--cap-drop ALL`; siguen sin red externa ni privilegios nuevos.
SERVICE_HARDENING = [
    "--security-opt", "no-new-privileges",
    "--memory", "1g",
    "--pids-limit", "512",
]
SERVICE_BUDGET = 90.0  # segundos para que los servicios queden sanos


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
    extra: tuple[str, ...] = ()  # contenedores de los servicios del compose


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


def _build_repo_image(context: Path, dockerfile: str, image: str) -> None:
    """Imagen con el Dockerfile del repo. El contexto es el árbol del PR (no confiable)."""
    df = _plain_path(context, dockerfile)
    if not df.is_file():
        raise SandboxError(f"no existe el Dockerfile {dockerfile}")
    with _image_lock:
        _docker("build", "-q", "-f", str(df), "-t", image, str(context))


def _wait_services(names: list[tuple[str, bool]], budget: float) -> None:
    """Espera a que cada servicio esté sano (si tiene healthcheck) o corriendo."""
    deadline = time.monotonic() + budget
    for name, has_health in names:
        while True:
            fmt = "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}"
            status, _, health = _docker("inspect", "-f", fmt, name, timeout=30).stdout.strip().partition(" ")
            if status in ("exited", "dead"):
                logs = _docker("logs", "--tail", "20", name, check=False, timeout=30)
                raise SandboxError(f"el servicio {name} terminó: {(logs.stdout + logs.stderr).strip()[-400:]}")
            if (health == "healthy") if has_health else status == "running":
                break
            if time.monotonic() > deadline:
                raise SandboxError(f"el servicio {name} no quedó sano a tiempo")
            time.sleep(1)


def _run_seed(run_id: str, base_tree: Path, build, app_env: dict[str, str], seed_env: dict[str, str],
              command: tuple[str, ...]) -> None:
    """Corre la semilla con la imagen de la app de la base, en la red de la corrida."""
    tag = f"{run_id}-seed"
    try:
        build(base_tree, tag)
        cmd = ["run", "--rm", "--name", tag, "--network", run_id, *HARDENING]
        for k, v in {**app_env, **seed_env}.items():
            cmd += ["-e", f"{k}={v}"]
        try:
            proc = _docker(*cmd, tag, *command, timeout=SERVICE_BUDGET, check=False)
        except subprocess.TimeoutExpired:
            _docker("rm", "-f", tag, check=False, timeout=60)
            raise SandboxError("la semilla no terminó a tiempo") from None
        if proc.returncode != 0:
            raise SandboxError(f"la semilla falló ({proc.returncode}): {(proc.stdout + proc.stderr).strip()[-400:]}")
    finally:
        _remove_image(tag)


def start_app(context: Path, case: Case | Target, compose_text: str | bytes | None = None,
              seed_context: Path | None = None, seed_env: dict[str, str] | None = None) -> AppHandle:
    """Construye y levanta la app de `context` en un contenedor aislado, sin salida a internet.

    Con `app.compose`, levanta antes los servicios del compose en la misma red
    interna. `compose_text` es el compose de la rama base; si no se pasa, se lee
    de `context` (solo para casos locales confiables).

    Si la configuración trae `seed`, después de los servicios y antes de la app
    corre la semilla con una imagen construida desde `seed_context` (el árbol de
    la base, nunca el del PR) y las variables `seed_env` (las marcas canario).
    """
    spec = case.app
    run_id = f"cp-{uuid.uuid4().hex[:10]}"
    image = f"{run_id}-app"
    container = f"{run_id}-app"
    plan = None
    if spec.compose is not None:
        try:
            text = compose_text if compose_text is not None else _plain_path(context, spec.compose.file).read_bytes()
            plan = compose_mod.parse(text, spec.compose.service)
        except (compose_mod.ComposeError, OSError) as e:
            raise SandboxError(f"compose inválido: {e}") from e

    def build(root: Path = context, tag: str = image) -> None:
        if plan is not None:
            ctx = root if plan.app.build_context == "." else _plain_path(root, plan.app.build_context)
            _build_repo_image(ctx, plan.app.dockerfile or "Dockerfile", tag)
        elif spec.dockerfile is not None:
            _build_repo_image(root, spec.dockerfile, tag)
        else:
            _build_app_image(root, tag)

    seed_cmd = tuple(getattr(case, "seed", ()) or ())
    if seed_cmd and seed_context is None:
        raise SandboxError("la configuración pide semilla pero no se pasó el árbol de la base")

    if spec.module is not None:
        command = ["uvicorn", spec.module, "--host", "0.0.0.0", "--port", str(spec.port)]
    else:
        command = list(plan.app.command) if plan else []
    env = dict(plan.app.environment) if plan else {}
    env.update(dict(spec.env))
    aliases = ["--network-alias", "app"] + (["--network-alias", plan.app.name] if plan else [])
    cmd = ["run", "-d", "--name", container, "--network", run_id, *aliases, *HARDENING]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [image, *command]

    extra: list[str] = []
    handle = AppHandle(run_id, run_id, container, image, "", tuple(extra))
    try:
        build()
        _docker("network", "create", "--internal", run_id)
        if plan is not None:
            waits = []
            for svc in plan.services:
                name = f"{run_id}-{svc.name}"
                extra.append(name)
                handle.extra = tuple(extra)
                scmd = ["run", "-d", "--name", name, "--network", run_id, "--network-alias", svc.name, *SERVICE_HARDENING]
                for k, v in svc.environment:
                    scmd += ["-e", f"{k}={v}"]
                if svc.health_cmd:
                    scmd += ["--health-cmd", svc.health_cmd, "--health-interval", "1s", "--health-timeout", "5s",
                             "--health-retries", "90", "--health-start-period", "1s"]
                _docker("pull", "-q", svc.image, check=False, timeout=600)
                _docker(*scmd, svc.image, *svc.command)
                waits.append((name, bool(svc.health_cmd)))
            _wait_services(waits, SERVICE_BUDGET)
        if seed_cmd:
            _run_seed(run_id, seed_context, build, env, seed_env or {}, seed_cmd)
        try:
            _docker(*cmd)
        except SandboxError as e:
            if not any(m in str(e) for m in MISSING_IMAGE):
                raise
            _docker("rm", "-f", container, check=False, timeout=60)
            build()  # otra corrida fuera de este proceso la borró
            _docker(*cmd)
    except SandboxError:
        stop(handle)
        raise
    return AppHandle(run_id, run_id, container, image, f"http://app:{spec.port}", tuple(extra))


INCOMPLETE = {"schema": 1, "status": "incomplete", "suites": {}, "attacks": None}


def _run_trusted(app: AppHandle, name: str, mounts: list[tuple[Path, str]], args: list[str], budget: float,
                 entrypoint: list[str] | None = None) -> tuple[int | None, str, str]:
    """Corre la imagen de Cachai en la red de la app. Devuelve (código, stdout, stderr);
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
        budget, entrypoint=["python", "-m", "cachai.redteam"],
    )
    data = _last_json(out) if code == 0 else None
    if data is None or "attacks" not in data:
        raise SandboxError(f"el equipo rojo no terminó: {(out + err).strip()[-500:]}")
    return data["attacks"]


def stop(app: AppHandle) -> None:
    _docker("rm", "-f", app.container, *app.extra, check=False, timeout=60)
    _docker("network", "rm", app.network, check=False, timeout=60)
    _remove_image(app.image)
