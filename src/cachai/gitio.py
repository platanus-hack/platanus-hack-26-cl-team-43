"""Lectura del repo que se revisa.

Todo se lee de los objetos de git por su hash, nunca del directorio de trabajo.
Todas las invocaciones comparten una política (`git_cmd`, `git_env`): no se
obedecen los reemplazos de objetos (`refs/replace`), que harían que un hash
muestre el contenido de otro commit, y se rechaza un repo con injertos
(`info/grafts`), que alteran la ascendencia. Tampoco corren hooks ni fsmonitor.

Límite: el `.git` local lo aporta quien opera Cachai. Esto no defiende
contra una base de objetos manipulada a mano; en producción se revisa un clon
limpio del remoto.
La exportación escribe exactamente el árbol del commit: no usa `git archive`
porque ese comando obedece los `.gitattributes` del propio árbol, y un PR podría
marcar archivos con `export-ignore` para que el código evaluado no sea el mismo
que se integra.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath

SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
MAX_BLOB = 50 * 1024 * 1024


class GitError(RuntimeError):
    pass


def git_cmd(repo: str | Path, *args: str) -> list[str]:
    """Línea de comandos de git con la política común de lectura."""
    return [
        "git", "--no-replace-objects", "-C", str(repo),
        "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *args,
    ]


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return env


def _run(repo: str | Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(git_cmd(repo, *args), capture_output=True, text=text, env=git_env())


def _git(repo: str | Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    proc = _run(repo, *args, text=text)
    if proc.returncode != 0:
        err = proc.stderr if text else proc.stderr.decode(errors="replace")
        raise GitError(f"git {args[0]} falló: {err.strip()[-300:]}")
    return proc


def check_repo(repo: str | Path) -> None:
    """Rechaza repos cuya ascendencia está alterada por injertos locales."""
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    if (common / "info" / "grafts").exists():
        raise GitError("el repo tiene injertos (info/grafts): la ascendencia no es confiable")


def resolve(repo: str | Path, ref: str) -> str:
    """Convierte una rama o referencia en el hash de su commit."""
    if not isinstance(ref, str) or not ref or ref.startswith("-") or any(c in ref for c in "\n\r\0 "):
        raise GitError(f"referencia inválida: {ref!r}")
    check_repo(repo)
    sha = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}").stdout.strip()
    if not SHA_RE.match(sha):
        raise GitError(f"no se pudo resolver {ref!r}")
    return sha


def _check_sha(sha: str) -> str:
    if not SHA_RE.match(sha):
        raise GitError(f"se esperaba un hash de commit: {sha!r}")
    return sha


def is_ancestor(repo: str | Path, ancestor: str, descendant: str) -> bool:
    proc = _run(repo, "merge-base", "--is-ancestor", _check_sha(ancestor), _check_sha(descendant))
    if proc.returncode not in (0, 1):
        raise GitError("git merge-base falló")
    return proc.returncode == 0


def changed_files(repo: str | Path, base: str, head: str) -> list[tuple[str, str]]:
    """Devuelve (estado, ruta) de cada archivo que cambia entre dos commits.

    Sin detección de renombres: mover un archivo aparece como borrado más
    agregado, para que sacar un test de su carpeta no pase inadvertido.
    """
    out = _git(
        repo, "diff", "--name-status", "--no-renames", "--no-ext-diff", "-z", _check_sha(base), _check_sha(head), text=False
    ).stdout
    parts = out.split(b"\0")
    result = []
    for i in range(0, len(parts) - 1, 2):
        result.append((parts[i].decode()[:1], parts[i + 1].decode(errors="surrogateescape")))
    return result


def ls_tree(repo: str | Path, sha: str) -> list[tuple[str, str, str, str]]:
    """Entradas (modo, tipo, hash, ruta) del árbol completo de un commit."""
    out = _git(repo, "ls-tree", "-r", "-z", "--full-tree", _check_sha(sha), text=False).stdout
    entries = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        meta, path = raw.split(b"\t", 1)
        mode, typ, oid = meta.decode().split(" ")
        entries.append((mode, typ, oid, path.decode(errors="surrogateescape")))
    return entries


def read_blob(repo: str | Path, sha: str, path: str) -> bytes | None:
    """Contenido de `path` en el commit `sha`, o None si no existe o no es un archivo."""
    safe_tree_path(path)
    proc = _run(repo, "cat-file", "blob", f"{_check_sha(sha)}:{path}", text=False)
    return proc.stdout if proc.returncode == 0 else None


def safe_tree_path(path: str) -> PurePosixPath:
    p = PurePosixPath(path)
    if not path or p.is_absolute() or "\\" in path or "\0" in path:
        raise GitError(f"ruta inválida en el árbol: {path!r}")
    if any(part in ("..", ".") or part.lower() == ".git" for part in p.parts):
        raise GitError(f"ruta reservada en el árbol: {path!r}")
    return p


def export_tree(repo: str | Path, sha: str, dest: str | Path) -> Path:
    """Escribe en `dest` (vacío) el árbol exacto del commit `sha`.

    Archivos regulares primero y enlaces simbólicos al final, para que ninguna
    escritura pase a través de un enlace. Los submódulos se rechazan.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise GitError(f"{dest} no está vacío")
    entries = ls_tree(repo, sha)
    for mode, typ, _, path in entries:
        safe_tree_path(path)
        if typ != "blob" or mode not in ("100644", "100755", "120000"):
            raise GitError(f"entrada no soportada en el árbol: {mode} {typ} {path}")

    proc = subprocess.Popen(git_cmd(repo, "cat-file", "--batch"), stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=git_env())
    assert proc.stdin and proc.stdout
    links: list[tuple[str, bytes]] = []
    try:
        for mode, _, oid, path in entries:
            proc.stdin.write(oid.encode() + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise GitError(f"objeto inesperado para {path}")
            size = int(header[2])
            if size > MAX_BLOB:
                raise GitError(f"archivo demasiado grande: {path}")
            data = proc.stdout.read(size)
            proc.stdout.read(1)
            if mode == "120000":
                links.append((path, data))
                continue
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755 if mode == "100755" else 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
    finally:
        proc.stdin.close()
        proc.stdout.close()
        proc.wait()
    for path, data in links:
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(target):
            raise GitError(f"ruta duplicada en el árbol: {path}")
        os.symlink(os.fsdecode(data), target)
    return dest
