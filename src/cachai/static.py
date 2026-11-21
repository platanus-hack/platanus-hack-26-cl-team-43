"""Análisis estático de las líneas que cambia el PR, como nota.

El equipo rojo prueba fallas con pedidos reales; el análisis estático encuentra
patrones que el equipo rojo no ataca (una consulta armada con texto, un
`subprocess` con `shell=True`, `pickle` con datos de afuera). Da falsos
positivos, así que nunca es motivo de rechazo: sale como nota con archivo y
línea.

Corre bandit desde la imagen del evaluador, en un contenedor sin red ni
privilegios, con solo los archivos Python que el PR cambia montados de solo
lectura. Bandit lee el código, no lo ejecuta. Se quedan los hallazgos de
severidad media o alta que caen en líneas agregadas o cambiadas por el PR.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path, PurePosixPath

from . import gitio

SEVERITIES = ("MEDIUM", "HIGH")
MAX_NOTES = 8
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(repo: str | Path, base_sha: str, pr_sha: str, path: str) -> set[int]:
    """Líneas del archivo en el PR que el diff agrega o cambia."""
    out = gitio._git(repo, "diff", "-U0", "--no-ext-diff", "--no-renames", gitio._check_sha(base_sha),
                     gitio._check_sha(pr_sha), "--", path).stdout
    lines: set[int] = set()
    for ln in out.splitlines():
        m = HUNK.match(ln)
        if m:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            lines.update(range(start, start + count))
    return lines


def targets(repo: str | Path, base_sha: str, pr_sha: str, tests_dir: str) -> dict[str, set[int]]:
    """Archivos Python de la app que el PR agrega o cambia, con sus líneas cambiadas."""
    out = {}
    for status, path in gitio.changed_files(repo, base_sha, pr_sha):
        p = PurePosixPath(path)
        if status == "D" or p.suffix != ".py" or p.parts[: len(PurePosixPath(tests_dir).parts)] == PurePosixPath(tests_dir).parts:
            continue
        lines = changed_lines(repo, base_sha, pr_sha, path)
        if lines:
            out[path] = lines
    return out


def parse(report: dict, wanted: dict[str, set[int]], prefix: str = "/code/") -> list[str]:
    """Notas de los hallazgos de bandit que caen en líneas cambiadas."""
    notes = []
    for r in sorted(report.get("results") or [], key=lambda r: (r.get("filename", ""), r.get("line_number", 0))):
        path = str(r.get("filename", "")).removeprefix(prefix)
        line = r.get("line_number")
        if r.get("issue_severity") not in SEVERITIES or line not in wanted.get(path, ()):
            continue
        notes.append(f"análisis estático (bandit {r.get('test_id')}, severidad {r.get('issue_severity', '').lower()}): "
                     f"{path}:{line} {str(r.get('issue_text', '')).strip()}")
    if len(notes) > MAX_NOTES:
        notes = notes[:MAX_NOTES] + [f"análisis estático: {len(notes) - MAX_NOTES} hallazgos más en el reporte completo"]
    return notes


def scan(repo: str | Path, base_sha: str, pr_sha: str, pr_tree: Path, tests_dir: str, work: Path) -> list[str]:
    """Corre bandit sobre los archivos cambiados del PR y devuelve las notas. Nunca bloquea."""
    from . import sandbox

    wanted = targets(repo, base_sha, pr_sha, tests_dir)
    if not wanted:
        return []
    code = work / "estatico"
    for path in wanted:
        src = pr_tree / path
        if src.is_symlink() or not src.is_file():
            continue
        dst = code / path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    image = sandbox.build_evaluator_image()
    proc = sandbox._docker(
        "run", "--rm", "--network", "none", *sandbox.HARDENING, "-v", f"{code.resolve()}:/code:ro",
        "--entrypoint", "bandit", image, "-q", "-r", "-f", "json", "/code",
        timeout=120, check=False,
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [f"análisis estático: bandit no corrió ({(proc.stderr or proc.stdout).strip()[-160:]})"]
    return parse(report, wanted)
