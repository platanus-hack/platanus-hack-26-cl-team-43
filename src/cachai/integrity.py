"""Detección de cambios que alteran cómo se evalúa el código.

Restaurar los tests de la rama base no basta: un hook nuevo puede reescribir
los resultados de pytest. Este módulo lista los cambios del PR que tocan la
evaluación.

Cada marca tiene una severidad:

- `block`: el PR no entra (hooks de evaluación, configuración de pytest, tests
  existentes modificados o borrados, código de la app que referencia pytest).
- `info`: se informa y no bloquea (tests nuevos, que no se usan para evaluar;
  dependencias cambiadas).

La revisión estática es evidencia, no garantía: se puede esquivar ofuscando el
código. La garantía es que el evaluador corre en otro contenedor.
"""

from __future__ import annotations

import ast
import configparser
import fnmatch
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import gitio

# Configuración de la evaluación: solo vale la de la rama base.
EVAL_CONFIG = ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", ".cachai.json")
# De estas, las que además configuran otras cosas: solo bloquea el cambio en su sección de pytest.
SHARED_CONFIG = ("pyproject.toml", "setup.cfg", "tox.ini")

# Archivos que, si aparecen o cambian, alteran la ejecución de Python o de pytest.
EVAL_HOOK_PATTERNS = ("conftest.py", "sitecustomize.py", "usercustomize.py", "*.pth")

# Con `on: pull_request` el workflow sale del commit del PR: cambiarlo cambia quién corre la revisión.
CI_DIRS = (".github/workflows", ".github/actions")

# Scripts que corren al instalar un paquete: código del PR que se ejecuta al construir la imagen.
INSTALL_SCRIPTS = ("setup.py",)

DEPENDENCY_PATTERNS = ("requirements*.txt", "Pipfile", "Pipfile.lock", "poetry.lock", "uv.lock")

PYTEST_REF = re.compile(r"(?<![A-Za-z0-9])_?pytest(?![A-Za-z0-9])|(?<![A-Za-z0-9])pluggy(?![A-Za-z0-9])|pytest_", re.IGNORECASE)
MAX_SCAN_BYTES = 1_000_000
MAX_HITS_PER_FILE = 3


@dataclass(frozen=True)
class Flag:
    path: str
    status: str  # A (nuevo), M (modificado), D (borrado)
    reason: str
    severity: str = "block"  # block | info
    line: int | None = None
    detail: str = ""

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        tail = f" — {self.detail}" if self.detail else ""
        return f"{self.reason} ({self.status}): {where}{tail}"


def is_hook(path: str) -> bool:
    return any(fnmatch.fnmatch(PurePosixPath(path).name, pat) for pat in EVAL_HOOK_PATTERNS)


def in_dir(path: str, directory: str) -> bool:
    d = PurePosixPath(directory).parts
    return PurePosixPath(path).parts[: len(d)] == d


def classify(changes: list[tuple[str, str]], test_dirs: tuple[str, ...] = ("tests",),
             eval_files: tuple[str, ...] = (), app_files: tuple[str, ...] = ()) -> list[Flag]:
    """Marca los cambios por nombre y ubicación, sin mirar el contenido (conservador).

    `eval_files`: archivos que la configuración de la base usa para evaluar (el
    compose): cambiarlos bloquea. `app_files`: los que construyen la app (su
    Dockerfile): se informa.
    """
    flags = []
    for status, path in changes:
        name = PurePosixPath(path).name
        if path in eval_files:
            flags.append(Flag(path, status, "compose de la evaluación (se usa el de la base)"))
        elif path in app_files:
            flags.append(Flag(path, status, "Dockerfile de la app cambiado: la imagen se construye con el del PR", "info"))
        elif is_hook(path):
            flags.append(Flag(path, status, "hook de evaluación"))
        elif any(in_dir(path, d) for d in CI_DIRS):
            flags.append(Flag(path, status, "workflow de CI que corre la revisión"))
        elif name in EVAL_CONFIG:
            flags.append(Flag(path, status, "configuración de pytest"))
        elif any(in_dir(path, d) for d in test_dirs):
            if status == "A":
                flags.append(Flag(path, status, "tests nuevos, no se usan para evaluar", "info"))
            else:
                flags.append(Flag(path, status, "tests existentes modificados"))
        elif name in INSTALL_SCRIPTS and status != "D":
            flags.append(Flag(path, status, "script de instalación, corre al construir la imagen"))
        elif any(fnmatch.fnmatch(name, pat) for pat in DEPENDENCY_PATTERNS):
            flags.append(Flag(path, status, "dependencias cambiadas, se instalan al construir la imagen", "info"))
    return flags


def _pytest_section(name: str, data: bytes | None) -> object:
    """Parte del archivo que configura pytest. Lanza ValueError si no se puede leer."""
    if data is None:
        return None
    try:
        text = data.decode()
        if name == "pyproject.toml":
            return tomllib.loads(text).get("tool", {}).get("pytest")
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_string(text)
        section = "tool:pytest" if name == "setup.cfg" else "pytest"
        return dict(cp[section]) if cp.has_section(section) else None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, configparser.Error) as e:
        raise ValueError(str(e)) from e


def _code_refs(source: str) -> dict[int, str] | None:
    """Líneas donde el código (no comentarios ni docstrings) nombra pytest.

    Cuenta imports, nombres, atributos y textos que no sean docstrings, porque
    un hook puede cargar el módulo por nombre (`sys.modules["pytest"]`).
    Devuelve None si el archivo no se puede analizar.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    hits: dict[int, str] = {}
    for node in ast.walk(tree):
        texts: list[str] = []
        if isinstance(node, ast.Import):
            texts = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            texts = [node.module or ""] + [a.name for a in node.names]
        elif isinstance(node, ast.Name):
            texts = [node.id]
        elif isinstance(node, ast.Attribute):
            texts = [node.attr]
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)) and id(node) not in docstrings:
            texts = [node.value if isinstance(node.value, str) else node.value.decode(errors="replace")]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            texts = [node.name]
        if any(PYTEST_REF.search(t) for t in texts):
            hits.setdefault(node.lineno, "")
    return hits


def _scan_pytest_refs(path: str, status: str, pr: bytes, base: bytes | None) -> list[Flag]:
    if len(pr) > MAX_SCAN_BYTES:
        return [Flag(path, status, "archivo de código demasiado grande para revisar")]
    source = pr.decode(errors="replace")
    lines = source.splitlines()
    known = set(base.decode(errors="replace").splitlines()) if base else set()
    refs = _code_refs(source)
    if refs is None:
        # No se pudo analizar: se revisa línea por línea, de forma conservadora.
        refs = {i: "" for i, line in enumerate(lines, 1) if PYTEST_REF.search(line)}
    flags = []
    for i in sorted(refs):
        line = lines[i - 1] if 0 < i <= len(lines) else ""
        if line in known:
            continue
        flags.append(Flag(path, status, "código de la app que referencia pytest", line=i, detail=line.strip()[:120]))
        if len(flags) == MAX_HITS_PER_FILE:
            break
    return flags


def _dependency_diff(base: bytes | None, pr: bytes | None, limit: int = 6) -> str:
    """Líneas agregadas y quitadas de un archivo de dependencias, para la nota."""
    def lines(b: bytes | None) -> list[str]:
        return [ln.strip() for ln in (b or b"").decode(errors="replace").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    old, new = lines(base), lines(pr)
    changes = [f"+{ln}" for ln in new if ln not in old] + [f"-{ln}" for ln in old if ln not in new]
    more = f" y {len(changes) - limit} más" if len(changes) > limit else ""
    return ", ".join(changes[:limit]) + more


def scan(repo: str | Path, base_sha: str, pr_sha: str, tests_dir: str = "tests",
         eval_files: tuple[str, ...] = (), app_files: tuple[str, ...] = ()) -> list[Flag]:
    """Marcas de un PR: por nombre (`classify`) y por contenido."""
    changes = gitio.changed_files(repo, base_sha, pr_sha)
    flags = []
    for f in classify(changes, (tests_dir,), eval_files, app_files):
        name = PurePosixPath(f.path).name
        if f.reason == "configuración de pytest" and name in SHARED_CONFIG:
            try:
                same = _pytest_section(name, gitio.read_blob(repo, base_sha, f.path)) == _pytest_section(
                    name, gitio.read_blob(repo, pr_sha, f.path)
                )
            except ValueError:
                same = False
            if same:
                f = Flag(f.path, f.status, "configuración compartida, la sección de pytest no cambia", "info")
        if f.reason.startswith("dependencias cambiadas"):
            f = Flag(f.path, f.status, f.reason, f.severity, detail=_dependency_diff(
                gitio.read_blob(repo, base_sha, f.path), gitio.read_blob(repo, pr_sha, f.path)))
        flags.append(f)
    for status, path in changes:
        if status == "D" or not path.endswith(".py") or in_dir(path, tests_dir) or is_hook(path):
            continue
        pr_blob = gitio.read_blob(repo, pr_sha, path)
        if pr_blob is None:
            continue
        flags.extend(_scan_pytest_refs(path, status, pr_blob, gitio.read_blob(repo, base_sha, path)))
    return flags
