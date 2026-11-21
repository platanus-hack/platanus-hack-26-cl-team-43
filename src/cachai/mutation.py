"""Aviso de tests débiles: mutantes de seguridad sobre la app de la base.

Es una nota, nunca un rechazo. La idea: si los tests de la base (y los
reservados) no se dan cuenta de que se quitó un control de autorización, esos
tests comprueban menos de lo que parecen. Cachai lo dice en el reporte; la
decisión no cambia.

Los mutantes no se inventan sobre el código del PR (que es lo que se está
revisando) sino sobre la app de la **rama base**, una vez por commit de base:

- `auth`: se quita una llamada de control de sesión (`current_user(...)`,
  `require_login(...)`), la que sea, en una función.
- `owner`: se desactiva la condición de un `if` que responde 401/403/404 y
  menciona dueño, usuario, sesión o autenticación.

Cada mutante se levanta como la app de la base y se le corren los tests de la
base y los funcionales reservados. Si todos pasan, el mutante **sobrevive** y
se informa. Si la app mutada no arranca, no cuenta: no hay evidencia.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import sandbox
from .case import Target

MAX_MUTANTS = 8
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".cachai", "migrations", "alembic"}
AUTH_CALL = re.compile(r"(?:^|_)(?:auth|authenticate|authorize|login|session|token|user|current_user|require|permission|admin)(?:$|_)", re.I)
GUARD_WORDS = re.compile(r"\b(owner|user|current|auth|uid|account|session|sub|tenant|permission)\b", re.I)
ERROR_STATUS = {401, 403, 404}


@dataclass(frozen=True)
class Mutant:
    kind: str  # auth | owner
    file: str  # ruta relativa al árbol
    line: int
    function: str
    what: str  # texto para el reporte, sin ids ni marcas

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.file}:{self.line}"


def _is_error_guard(node: ast.If) -> bool:
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.Raise):
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Constant) and sub.value in ERROR_STATUS:
                    return True
        if isinstance(stmt, ast.Return):
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Constant) and sub.value in ERROR_STATUS:
                    return True
    return False


class _Find(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found: list[Mutant] = []
        self.function = "<module>"

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        previous = self.function
        self.function = node.name
        self.generic_visit(node)
        self.function = previous

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Expr(self, node: ast.Expr) -> None:
        call = node.value
        if isinstance(call, ast.Call):
            name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
            if name and AUTH_CALL.search(name):
                self.found.append(Mutant("auth", "", node.lineno, self.function,
                                         f"quita el control de sesión `{name}(...)` en `{self.function}`"))
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        if _is_error_guard(node) and GUARD_WORDS.search(ast.unparse(node.test)):
            condition = " ".join(ast.unparse(node.test).split())[:80]
            self.found.append(Mutant("owner", "", node.lineno, self.function,
                                     f"desactiva la comprobación `{condition}` en `{self.function}`"))
        self.generic_visit(node)


def mutants(tree: str | Path, limit: int = MAX_MUTANTS) -> list[Mutant]:
    """Mutantes de seguridad del árbol, en orden de archivo y línea."""
    root = Path(tree)
    out: list[Mutant] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts) or rel.parts[0] in ("tests", "test"):
            continue
        try:
            source = path.read_text()
            node = ast.parse(source)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        finder = _Find()
        finder.visit(node)
        out += [Mutant(m.kind, str(rel), m.line, m.function, m.what) for m in finder.found]
    unique: dict[tuple[str, int], Mutant] = {}
    for m in out:
        unique.setdefault((m.file, m.line), m)
    return sorted(unique.values(), key=lambda m: (m.file, m.line))[:limit]


class _Mutate(ast.NodeTransformer):
    """Aplica un mutante. `seen` cuenta las llamadas para no tocar otra línea."""

    def __init__(self, mutant: Mutant) -> None:
        self.mutant = mutant
        self.done = False
        self.function = "<module>"

    def _visit_function(self, node):
        previous = self.function
        self.function = node.name
        self.generic_visit(node)
        self.function = previous
        return node

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def _hit(self, line: int) -> bool:
        return not self.done and line == self.mutant.line and self.function == self.mutant.function

    def visit_Expr(self, node):
        if self.mutant.kind == "auth" and self._hit(node.lineno) and isinstance(node.value, ast.Call):
            self.done = True
            return None
        return self.generic_visit(node)

    def visit_If(self, node):
        if self.mutant.kind == "owner" and self._hit(node.lineno):
            node.test = ast.Constant(value=False)
            self.done = True
            return node
        return self.generic_visit(node)


def apply(tree: str | Path, mutant: Mutant, dest: str | Path) -> Path:
    """Copia `tree` a `dest` (que no debe existir) con el mutante aplicado."""
    src, dst = Path(tree), Path(dest)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    target = dst / mutant.file
    node = ast.parse(target.read_text())
    node = _Mutate(mutant).visit(node)
    ast.fix_missing_locations(node)
    target.write_text(ast.unparse(node) + "\n")
    return dst


def cache_key(base_sha: str, target: Target, compose_text: bytes | str | None, with_holdout: bool) -> str:
    import hashlib
    if isinstance(compose_text, str):
        compose_text = compose_text.encode()
    data = {
        "v": 1,
        "base": base_sha,
        "target": target.to_dict(),
        "compose": hashlib.sha256(compose_text).hexdigest() if compose_text is not None else None,
        "code": sandbox.evaluator_tag(),
        "holdout": with_holdout,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _cache_file(key: str) -> Path:
    return sandbox.cache_dir() / "mutantes" / f"{key}.json"


def load_cached(key: str, base_sha: str) -> list[dict] | None:
    try:
        data = json.loads(_cache_file(key).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("base") != base_sha or not isinstance(data.get("survived"), list):
        return None
    return [d for d in data["survived"] if isinstance(d, dict) and isinstance(d.get("what"), str)]


def save_cached(key: str, base_sha: str, survived: list[dict]) -> None:
    f = _cache_file(key)
    try:
        f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump({"base": base_sha, "survived": survived}, out)
        os.replace(tmp, f)
    except OSError:
        pass


def _survived(result: object) -> bool:
    """El mutante sobrevive si el evaluador aprobó con todo en verde."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        return False
    suites = result.get("suites") or {}
    for name in ("base", "holdout"):
        suite = suites.get(name)
        if suite is None:
            continue
        if suite.get("ok") is not True:
            return False
    return True


def run(base_tree: Path, target: Target, workdir: Path, holdout_tests: Path | None, budget: float,
        tmp: Path, app_kw: dict, compose_text: bytes | str | None = None) -> list[dict]:
    """Corre cada mutante contra los tests de la base y los reservados.

    Devuelve los que sobreviven, con `what` para la nota. Un mutante cuya app no
    arranca o cuyo evaluador no termina no cuenta como superviviente.
    """
    found = mutants(base_tree)
    survived: list[dict] = []
    for i, mutant in enumerate(found):
        tree = apply(base_tree, mutant, tmp / f"mutante-{i}")
        app = None
        try:
            app = sandbox.start_app(tree, target, compose_text, **app_kw)
            _, result = sandbox.run_evaluator(app, target, workdir, holdout_tests, budget, None)
        except sandbox.SandboxError:
            continue
        finally:
            if app is not None:
                sandbox.stop(app)
        if _survived(result):
            survived.append({"what": mutant.what, "file": mutant.file, "line": mutant.line})
    return survived


def notes(base_tree: Path, target: Target, workdir: Path, holdout_tests: Path | None, budget: float,
          tmp: Path, app_kw: dict, base_sha: str, compose_text: bytes | str | None) -> list[str]:
    """Notas del reporte para esta base. Cachea por commit de base y código de Cachai."""
    key = cache_key(base_sha, target, compose_text, holdout_tests is not None)
    survived = load_cached(key, base_sha)
    if survived is None:
        survived = run(base_tree, target, workdir, holdout_tests, min(budget, 60.0), tmp, app_kw, compose_text)
        save_cached(key, base_sha, survived)
    total = len(mutants(base_tree))
    if not survived:
        return [f"pruebas de mutación: los tests de la base y los reservados matan los {total} mutantes de seguridad "
                "que se probaron (controles de sesión y de dueño)"]
    out = [f"pruebas de mutación: {len(survived)} de {total} mutantes de seguridad sobreviven; "
           "tus tests no detectan estos cambios (es una nota, no bloquea):"]
    out += [f"  - {d['what']} ({d['file']}:{d['line']})" for d in survived]
    return out
