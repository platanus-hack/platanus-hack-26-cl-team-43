"""Detección de cambios que alteran cómo se evalúa el código.

Restaurar los tests de la rama base no basta: un hook nuevo puede reescribir
los resultados de pytest. Este módulo lista los archivos del PR que tocan la
evaluación para restaurarlos o marcarlos.
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath

# Configuración que se restaura desde la rama base.
EVAL_CONFIG = ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")

# Archivos que, si aparecen o cambian, alteran la ejecución de Python o de pytest.
EVAL_HOOK_PATTERNS = ("conftest.py", "sitecustomize.py", "usercustomize.py", "*.pth")


@dataclass(frozen=True)
class Flag:
    path: str
    status: str  # A (nuevo), M (modificado), D (borrado), R (renombrado)
    reason: str


def changed_files(repo: str, base: str, head: str) -> list[tuple[str, str]]:
    """Devuelve (estado, ruta) de cada archivo que cambia entre base y head."""
    out = subprocess.run(
        ["git", "-C", repo, "diff", "--name-status", "-M", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    result = []
    for line in out.splitlines():
        parts = line.split("\t")
        status, path = parts[0][0], parts[-1]
        result.append((status, path))
    return result


def classify(changes: list[tuple[str, str]], test_dirs: tuple[str, ...] = ("tests",)) -> list[Flag]:
    """Marca los cambios que tocan tests, configuración o hooks de evaluación."""
    flags = []
    for status, path in changes:
        p = PurePosixPath(path)
        name = p.name
        if any(fnmatch.fnmatch(name, pat) for pat in EVAL_HOOK_PATTERNS):
            flags.append(Flag(path, status, "hook de evaluación"))
        elif name in EVAL_CONFIG:
            flags.append(Flag(path, status, "configuración de pytest"))
        elif p.parts and p.parts[0] in test_dirs:
            flags.append(Flag(path, status, "tests"))
    return flags
