"""App y evaluador en contenedores separados.

La app del PR corre en un contenedor; el evaluador, con los tests de la rama
base, corre en otro y le hace pedidos por HTTP. El código del PR nunca comparte
proceso ni contenedor con quien lo califica. Cada corrida tiene su propia red
Docker interna, sin salida a internet.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import compose as compose_mod
from .case import Case, Target
from .integrity import EVAL_CONFIG, is_hook

ROOT = Path(__file__).resolve().parents[2]
EVALUATOR_IMAGE = "cachai-evaluator"  # repositorio; la etiqueta la pone `evaluator_tag`
DEPS_IMAGE = "cachai-deps"  # repositorio; la etiqueta es el hash de los archivos de dependencias
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
SERVICE_BUDGET = 90.0
CANARY_ALIAS = "cachai-canario"  # segundos para que los servicios queden sanos


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
    temp_images: tuple[str, ...] = ()  # imágenes de dependencias de esta corrida, que se borran al parar


def cache_dir() -> Path:
    """Carpeta de la caché de Cachai, fuera de cualquier árbol que se revisa.

    Ningún contenedor la monta: el código del PR no la ve ni la escribe.
    """
    base = os.environ.get("CACHAI_CACHE_DIR") or str(
        Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "cachai")
    path = Path(base)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


# Dos revisiones del mismo árbol producen la misma imagen. Si una la borra justo
# cuando la otra termina de construirla, la segunda se queda sin imagen: construir
# y borrar se hacen de a uno.
_image_lock = threading.Lock()
MISSING_IMAGE = ("Unable to find image", "No such image", "pull access denied")


WHEELS_DIR = ".cachai-wheels"
NETWORK_ERRORS = ("Temporary failure in name resolution", "Network is unreachable", "Name or service not known",
                  "nodename nor servname", "Connection refused")
WHEEL_CACHE = Path(os.environ.get("CACHAI_WHEEL_CACHE", Path.home() / ".cache" / "cachai" / "wheels"))
# Lo que el build sin red necesita además de las dependencias: construir un paquete local sin internet.
BUILD_TOOLS = ("setuptools", "wheel")


def _local_requirement(line: str) -> bool:
    """Una línea que apunta a código del repo (se construye en el build, sin red), no a PyPI."""
    s = line.strip()
    return s.startswith(("-e", "--editable", ".", "/", "file:")) or s.startswith("-r") or s.startswith("-c")


def fetch_wheels(context: Path) -> Path:
    """Descarga como wheels las dependencias de `requirements.txt` del PR, en un contenedor aparte.

    `--only-binary=:all:` no corre ningún código del paquete: si una dependencia
    solo existe como fuente, la descarga falla y la revisión queda incompleta.
    Los wheels quedan en una caché por hash del archivo y se copian a
    `context/.cachai-wheels` para el build sin red.
    """
    req = _plain_path(context, "requirements.txt")
    if not req.is_file():
        raise SandboxError("falta requirements.txt en el árbol del PR")
    lines = req.read_text(errors="replace").splitlines()
    remote = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#") and not _local_requirement(ln)]
    text = "\n".join(remote + list(BUILD_TOOLS)) + "\n"
    key = hashlib.sha256(text.encode()).hexdigest()[:24]
    cache = WHEEL_CACHE / key
    if not (cache / ".completo").is_file():
        tmp = WHEEL_CACHE / f"{key}.{uuid.uuid4().hex[:6]}"
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "requirements.txt").write_text(text)
        proc = _docker(
            "run", "--rm", "--security-opt", "no-new-privileges", "--memory", "1g", "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp", "-v", f"{tmp.resolve()}:/wheels", "python:3.12-slim",
            "pip", "download", "--quiet", "--no-cache-dir", "--only-binary=:all:", "-d", "/wheels", "-r", "/wheels/requirements.txt",
            timeout=600, check=False,
        )
        if proc.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise SandboxError(f"no se pudieron descargar las dependencias como wheels (sin código de instalación): {proc.stderr.strip()[-400:]}")
        (tmp / "requirements.txt").unlink()
        (tmp / ".completo").write_text("")
        if cache.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            tmp.rename(cache)
    dest = context / WHEELS_DIR
    _remove(dest)  # una carpeta con este nombre que traiga el PR no se usa
    shutil.copytree(cache, dest)
    return dest


def _image_exists(image: str) -> bool:
    return _docker("image", "inspect", "-f", "{{.Id}}", image, check=False, timeout=60).returncode == 0


def _regular_bytes(tree: Path, name: str) -> bytes | None:
    """Contenido de `name` en `tree` si es un archivo regular (no un enlace)."""
    f = _plain_path(tree, name)
    if f.is_symlink() or not f.is_file():
        return None
    return f.read_bytes()


def deps_key(requirements: bytes, base_image_id: str = "") -> str:
    """Clave de la imagen de dependencias: su Dockerfile, la imagen de Python sobre
    la que se construye y los archivos de dependencias."""
    h = hashlib.sha256()
    h.update((ROOT / "docker" / "deps.Dockerfile").read_bytes())
    h.update(b"\0" + base_image_id.encode() + b"\0" + requirements + b"\0")
    return h.hexdigest()


_deps_locks: dict[str, threading.Lock] = {}


def _deps_image(context: Path, trusted: Path | None, image: str) -> tuple[str, bool]:
    """Imagen con las dependencias de `context` instaladas. Devuelve (etiqueta, es_temporal).

    Solo se cachea (etiqueta por hash) si el `requirements.txt` de `context` es
    idéntico al de `trusted`, el árbol de la rama base: la imagen compartida
    siempre sale de archivos de la base. Si el PR lo cambió, o no hay árbol de
    confianza, se construye una imagen temporal de esta corrida que se borra al
    terminar, y nunca se reutiliza.

    El contexto del build tiene solo el archivo de dependencias y sus wheels:
    nunca el código del PR.
    """
    requirements = _regular_bytes(context, "requirements.txt")
    if requirements is None:
        raise SandboxError("falta requirements.txt (o no es un archivo regular) en el árbol de la app")
    local = any(_local_requirement(ln) for ln in requirements.decode(errors="replace").splitlines())
    cacheable = (trusted is not None and not local
                 and _regular_bytes(trusted, "requirements.txt") == requirements)
    base_id = _docker("image", "inspect", "-f", "{{.Id}}", "python:3.12-slim", check=False, timeout=60).stdout.strip()
    tag = f"{DEPS_IMAGE}:{deps_key(requirements, base_id)[:24]}" if cacheable else f"{image}-deps"
    with _image_lock:
        lock = _deps_locks.setdefault(tag, threading.Lock())
    with lock:
        if cacheable and _image_exists(tag):
            return tag, False
        with tempfile.TemporaryDirectory(prefix="cachai-deps-") as tmp:
            ctx = Path(tmp) / "ctx"
            if local:
                # Dependencias que apuntan al código del repo: el paquete local se
                # construye en el build, así que el contexto necesita el árbol.
                _copy_regular_tree(context, ctx, skip_symlinks=True)
            else:
                ctx.mkdir()
                (ctx / "requirements.txt").write_bytes(requirements)
            fetch_wheels(ctx)
            with _image_lock:
                proc = _docker("build", "--progress", "plain", "--network", "none",
                               "-f", str(ROOT / "docker" / "deps.Dockerfile"), "-t", tag, str(ctx), check=False)
        if proc.returncode != 0:
            log = proc.stderr + proc.stdout
            raise SandboxError(f"docker build de dependencias falló ({log.strip()[-400:]})")
    return tag, not cacheable


def _build_app_image(context: Path, image: str, trusted: Path | None = None) -> str | None:
    """Imagen Python de Cachai: la de dependencias más el código, sin red.

    Con `trusted` (el árbol de la rama base), si los archivos de dependencias de
    `context` son los mismos se reutiliza la imagen de dependencias cacheada; si
    no, se construye una temporal que hay que borrar. Devuelve la etiqueta
    temporal que hay que borrar, o None si se usó la cacheada.

    Se construye desde una copia temporal del contexto, sin enlaces simbólicos:
    el árbol original (a veces una carpeta del repo, como la base de un caso) no
    se toca.
    """
    deps, temporary = _deps_image(context, trusted, image)
    try:
        with tempfile.TemporaryDirectory(prefix="cachai-build-") as tmp:
            ctx = Path(tmp) / "ctx"
            _copy_regular_tree(context, ctx, skip_symlinks=True)
            with _image_lock:
                proc = _docker("build", "--progress", "plain", "--network", "none", "--build-arg", f"DEPS_IMAGE={deps}",
                               "-f", str(ROOT / "docker" / "app.Dockerfile"), "-t", image, str(ctx), check=False)
        if proc.returncode != 0:
            log = proc.stderr + proc.stdout
            offline = any(m in log for m in NETWORK_ERRORS)
            why = "un script de instalación intentó usar la red y el build no la tiene" if offline else "falló la construcción"
            raise SandboxError(f"docker build falló ({why}): {log.strip()[-400:]}")
    except SandboxError:
        if temporary:
            _remove_image(deps)
        raise
    return deps if temporary else None


def _remove_image(image: str) -> None:
    with _image_lock:
        _docker("rmi", "-f", image, check=False, timeout=60)


_built: set[str] = set()
_build_lock = threading.Lock()


def evaluator_tag(root: Path = ROOT) -> str:
    """Tag de la imagen del evaluador con un hash de lo que entra en ella.

    Un tag fijo lo comparten todas las copias del repo en la máquina: una revisión
    de otra rama que reconstruye la imagen cambia el código con el que corre esta.
    """
    h = hashlib.sha256()
    files = [root / "pyproject.toml", root / "README.md", root / "docker" / "evaluator.Dockerfile"]
    files += sorted(f for f in (root / "src").rglob("*") if f.is_file() and "__pycache__" not in f.parts)
    for f in files:
        if f.is_file():
            h.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes() + b"\0")
    return f"{EVALUATOR_IMAGE}:{h.hexdigest()[:16]}"


_evaluator: dict[str, str] = {}


def build_evaluator_image(root: Path = ROOT) -> str:
    """Construye la imagen del evaluador una vez por proceso y por versión del código."""
    with _build_lock:
        tag = evaluator_tag(root)
        if _evaluator.get(str(root)) != tag:
            _docker("build", "-q", "-f", str(root / "docker" / "evaluator.Dockerfile"), "-t", tag, str(root))
            _evaluator[str(root)] = tag
            _built.add(str(root))
    return tag


def _copy_regular_tree(src: Path, dst: Path, skip_symlinks: bool = False) -> None:
    """Copia un árbol de archivos regulares. No sigue enlaces simbólicos: con
    `skip_symlinks` los omite; si no, son un error."""
    if src.is_symlink() or not src.is_dir():
        raise SandboxError(f"se esperaba una carpeta real: {src}")
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src, followlinks=False):
        if skip_symlinks:
            dirs[:] = [d for d in dirs if d != "__pycache__" and not (Path(root) / d).is_symlink()]
            files = [f for f in files if not (Path(root) / f).is_symlink()]
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


def build_target_image(context: Path, target: Case | Target, image: str, compose_text: str | bytes | None = None,
                       trusted_deps: Path | None = None) -> str | None:
    """Imagen de la app de `context` según la configuración: la de Cachai (Python), el
    Dockerfile del repo o el servicio de la app en el compose.

    Devuelve la etiqueta de la imagen temporal de dependencias que hay que borrar
    (solo con la imagen de Cachai), o None."""
    spec = target.app
    if spec.compose is not None:
        try:
            text = compose_text if compose_text is not None else _plain_path(context, spec.compose.file).read_bytes()
            plan = compose_mod.parse(text, spec.compose.service)
        except (compose_mod.ComposeError, OSError) as e:
            raise SandboxError(f"compose inválido: {e}") from e
        ctx = context if plan.app.build_context == "." else _plain_path(context, plan.app.build_context)
        _build_repo_image(ctx, plan.app.dockerfile or "Dockerfile", image)
        return None
    if spec.dockerfile is not None:
        _build_repo_image(context, spec.dockerfile, image)
        return None
    return _build_app_image(context, image, trusted_deps)


JEST = "./node_modules/.bin/jest"


def tests_command(target: Case | Target | None, tests_dir: str) -> list[str]:
    if target is not None and getattr(target, "tests_runner", "pytest") == "jest":
        return [JEST, "--ci", "--cacheDirectory=/tmp/jest", tests_dir]
    return ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", tests_dir]


def run_pytest_inside(context: Path, tests_dir: str, budget: float = 120.0, target: Case | Target | None = None,
                      trusted_deps: Path | None = None) -> dict:
    """Líneas base: corre los tests dentro del mismo contenedor que el código.

    Devuelve lo que vería un CI: `passed` es verdadero si los tests salieron con 0.
    Con `target`, la imagen y el runner salen de su configuración (p. ej. jest).
    `trusted_deps`: árbol de la base; con él se reutiliza la imagen de
    dependencias cacheada si el PR no las cambió.
    """
    run_id = f"cp-{uuid.uuid4().hex[:10]}"
    image = f"{run_id}-ci"
    temp: list[str] = []

    def build() -> None:
        if target is None:
            deps = _build_app_image(context, image, trusted_deps)
        else:
            deps = build_target_image(context, target, image, trusted_deps=trusted_deps)
        if deps:
            temp.append(deps)

    build()
    cmd = [
        "run", "--rm", "--name", run_id, "--network", "none", *HARDENING, "-e", "HOME=/tmp",
        image, *tests_command(target, tests_dir),
    ]
    try:
        try:
            proc = _docker(*cmd, timeout=budget, check=False)
            if proc.returncode == 125 and any(m in proc.stderr for m in MISSING_IMAGE):
                build()  # otra corrida fuera de este proceso la borró
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
        for deps in temp:
            _remove_image(deps)


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
              seed_context: Path | None = None, seed_env: dict[str, str] | None = None,
              trusted_deps: Path | None = None) -> AppHandle:
    """Construye y levanta la app de `context` en un contenedor aislado, sin salida a internet.

    Con `app.compose`, levanta antes los servicios del compose en la misma red
    interna. `compose_text` es el compose de la rama base; si no se pasa, se lee
    de `context` (solo para casos locales confiables).

    Si la configuración trae `seed`, después de los servicios y antes de la app
    corre la semilla con una imagen construida desde `seed_context` (el árbol de
    la base, nunca el del PR) y las variables `seed_env` (las marcas canario).

    `trusted_deps` es el árbol de la rama base: con la imagen por defecto, si los
    archivos de dependencias de `context` son los mismos, se usa la imagen de
    dependencias cacheada; si no, una temporal de esta corrida.
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

    temp: list[str] = []

    def build(root: Path = context, tag: str = image) -> None:
        if plan is not None:
            ctx = root if plan.app.build_context == "." else _plain_path(root, plan.app.build_context)
            _build_repo_image(ctx, plan.app.dockerfile or "Dockerfile", tag)
        elif spec.dockerfile is not None:
            _build_repo_image(root, spec.dockerfile, tag)
        else:
            deps = _build_app_image(root, tag, trusted_deps)
            if deps:
                temp.append(deps)
                handle.temp_images = tuple(temp)

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
    return AppHandle(run_id, run_id, container, image, f"http://app:{spec.port}", tuple(extra), tuple(temp))


INCOMPLETE = {"schema": 1, "status": "incomplete", "suites": {}, "attacks": None}


def _run_trusted(app: AppHandle, name: str, mounts: list[tuple[Path, str]], args: list[str], budget: float,
                 entrypoint: list[str] | None = None, aliases: tuple[str, ...] = ()) -> tuple[int | None, str, str]:
    """Corre la imagen de Cachai en la red de la app. Devuelve (código, stdout, stderr);
    código None si hubo que matarlo por tiempo."""
    cmd = ["run", "--rm", "--name", name, "--network", app.network, *HARDENING]
    for alias in aliases:
        cmd += ["--network-alias", alias]
    for src, dst in mounts:
        cmd += ["-v", f"{src.resolve()}:{dst}:ro"]
    if entrypoint:
        cmd += ["--entrypoint", entrypoint[0]]
    cmd += [_evaluator.get(str(ROOT)) or build_evaluator_image(), *(entrypoint[1:] if entrypoint else []), *args]
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
                  budget: float = 120.0, attacks_file: Path | None = None, skip_base: bool = False) -> tuple[int, dict]:
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
    if skip_base:
        args.append("--skip-base")
    code, out, err = _run_trusted(app, f"{app.run_id}-eval", mounts, args, budget)
    if code is None:
        return 2, {**INCOMPLETE, "errors": ["el evaluador no terminó a tiempo"]}
    result = _last_json(out)
    if result is None:
        return 2, {**INCOMPLETE, "errors": [f"salida ilegible del evaluador: {err.strip()[-500:]}"]}
    return code, result


def run_jest(app: AppHandle, target: Case | Target, base_tree: Path, budget: float = 120.0,
             compose_text: str | bytes | None = None) -> dict:
    """Tests de la base con jest, por HTTP contra la app del PR.

    Corren con la imagen construida desde el árbol de la base (con sus
    dependencias de desarrollo, su configuración de jest y sus tests), en otro
    contenedor de la misma red. Nada del PR entra en esa imagen.
    """
    from .evaluator import parse_junit, summarize

    tag = f"{app.run_id}-tests"
    with tempfile.TemporaryDirectory(prefix="cachai-jest-") as tmp:
        out = Path(tmp) / "out"
        out.mkdir()
        os.chmod(out, 0o777)  # el usuario de la imagen (p. ej. node) escribe el junit
        try:
            build_target_image(base_tree, target, tag, compose_text)
            cmd = ["run", "--rm", "--name", tag, "--network", app.network, *HARDENING,
                   "-v", f"{out.resolve()}:/out", "-e", f"BASE_URL={app.base_url}", "-e", "HOME=/tmp",
                   "-e", "JEST_JUNIT_OUTPUT_DIR=/out", "-e", "JEST_JUNIT_ADD_FILE_ATTRIBUTE=true",
                   "-e", "JEST_JUNIT_CLASSNAME={filepath}", "-e", "JEST_JUNIT_TITLE={title}",
                   # La carpeta va antes de las opciones: --reporters acepta varios valores y se la tragaría.
                   tag, JEST, target.tests_dir, "--ci", "--cacheDirectory=/tmp/jest", "--reporters=default",
                   "--reporters=jest-junit"]
            try:
                proc = _docker(*cmd, timeout=budget, check=False)
                code, timed_out, log = proc.returncode, False, proc.stdout + proc.stderr
            except subprocess.TimeoutExpired:
                _docker("rm", "-f", tag, check=False, timeout=60)
                code, timed_out, log = None, True, ""
            junit = out / "junit.xml"
            cases = parse_junit(junit) if junit.is_file() else []
        finally:
            _remove_image(tag)
    return summarize(cases, code, timed_out, log)


def run_redteam(app: AppHandle, case: Case | Target, inventory_file: Path, budget: float = 60.0,
                families: tuple[str, ...] | None = None, routes_file: Path | None = None,
                info: dict | None = None) -> list[dict]:
    """Genera y confirma variantes de ataque contra la app. `routes_file`: rutas leídas
    del código de esa app; en `info` quedan las que su OpenAPI no publica."""
    args = ["--base-url", app.base_url, "--health", case.app.health, "--inventory", "/work/inventory.json", "--budget", str(budget)]
    mounts = [(inventory_file, "/work/inventory.json")]
    if routes_file is not None:
        mounts.append((routes_file, "/work/routes.json"))
        args += ["--routes", "/work/routes.json"]
    if families:
        args += ["--families", ",".join(families)]
    nonce = uuid.uuid4().hex[:12] if not families or "cmdi" in families else ""
    if nonce:
        args += ["--cmdi-nonce", nonce]
    aliases: tuple[str, ...] = ()
    if not families or "ssrf" in families:
        # El canario de SSRF corre en este contenedor: la app lo alcanza por este nombre, dentro de la red interna.
        args += ["--canary-host", CANARY_ALIAS]
        aliases = (CANARY_ALIAS,)
    code, out, err = _run_trusted(
        app, f"{app.run_id}-red", mounts,
        args,
        budget, entrypoint=["python", "-m", "cachai.redteam"], aliases=aliases,
    )
    data = _last_json(out) if code == 0 else None
    if data is None or "attacks" not in data:
        raise SandboxError(f"el equipo rojo no terminó: {(out + err).strip()[-500:]}")
    attacks = list(data["attacks"])
    if info is not None:
        info["hidden_routes"] = data.get("hidden_routes") or []
    probes = data.get("cmdi_probes") or []
    if probes:
        # Inyección de comandos: Cachai mira desde el host qué archivos testigo aparecieron en la app.
        from .redteam import CMDI_DIR, cmdi_attacks
        ls = _docker("exec", app.container, "ls", "-1a", CMDI_DIR, check=False, timeout=30)
        if ls.returncode != 0:
            raise SandboxError(f"no se pudo inspeccionar {CMDI_DIR} en el contenedor de la app: {ls.stderr.strip()[-200:]}")
        present = set(ls.stdout.split())
        attacks += [a.to_dict() for a in cmdi_attacks(probes, present)]
    return attacks


def run_attacks_only(app: AppHandle, case: Case | Target, attacks_file: Path, budget: float = 60.0) -> dict:
    """Corre solo los ataques contra la app (sin suites). Devuelve el reporte de ataques."""
    args = ["--base-url", app.base_url, "--health", case.app.health, "--attacks", "/work/attacks.json",
            "--budget", str(budget), "--attacks-only"]
    code, out, err = _run_trusted(app, f"{app.run_id}-atk", [(attacks_file, "/work/attacks.json")], args, budget)
    data = _last_json(out) if code == 0 else None
    if data is None or not isinstance(data.get("attacks"), dict):
        raise SandboxError(f"los ataques no terminaron: {(out + err).strip()[-500:]}")
    return data["attacks"]


def run_probe(app: AppHandle, case: Case | Target, users_file: Path, budget: float = 60.0) -> dict:
    """Lee `/openapi.json` de la app y lo que cada usuario de prueba ve en sus listados (para `cachai init`)."""
    args = ["--base-url", app.base_url, "--health", case.app.health, "--inventory", "/work/users.json",
            "--budget", str(budget), "--probe"]
    code, out, err = _run_trusted(app, f"{app.run_id}-probe", [(users_file, "/work/users.json")], args, budget,
                                  entrypoint=["python", "-m", "cachai.redteam"])
    data = _last_json(out) if code == 0 else None
    if data is None or "openapi" not in data:
        raise SandboxError(f"no se pudo leer la app: {(out + err).strip()[-500:]}")
    return data


def stop(app: AppHandle) -> None:
    _docker("rm", "-f", app.container, *app.extra, check=False, timeout=60)
    _docker("network", "rm", app.network, check=False, timeout=60)
    _remove_image(app.image)
    for image in app.temp_images:
        _remove_image(image)
