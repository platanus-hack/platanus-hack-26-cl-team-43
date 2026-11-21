"""Casos de evaluación: carga de `case.json` y materialización como repo git.

Un caso guarda la app de la rama base en `base/` y cada PR como una capa de
archivos en `prs/<nombre>/`. `materialize` arma un repo git temporal con `main`
y una rama por PR, que es lo que revisa Contraprueba.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .holdout import Attack, load_attacks, split

PR_KINDS = ("cheat", "honest", "half_patch", "correct_patch")
CASE_KEYS = {"id", "description", "app", "tests_dir", "attacks", "holdout_tests", "split", "prs"}
APP_KEYS = {"module", "port", "health"}
PR_KEYS = {"name", "kind", "description", "delete"}
SPLIT_KEYS = {"shown", "seed"}
CONFIG_FILE = ".contraprueba.json"  # configuración de la evaluación; solo vale la de la rama base
CONFIG_KEYS = {"app", "tests_dir"}
BRANCH_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
MODULE_RE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


@dataclass(frozen=True)
class AppSpec:
    module: str  # objeto ASGI, p. ej. "invoices.main:app"
    port: int = 8000
    health: str = "/health"

    def __post_init__(self) -> None:
        if not isinstance(self.module, str) or not MODULE_RE.match(self.module):
            raise ValueError(f"módulo ASGI inválido: {self.module!r}")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError(f"puerto inválido: {self.port!r}")
        if not isinstance(self.health, str) or not self.health.startswith("/") or self.health.startswith("//"):
            raise ValueError(f"ruta de salud inválida: {self.health!r}")


@dataclass(frozen=True)
class Target:
    """Qué se evalúa y cómo. Viene de la rama base o del caso, nunca del PR."""

    app: AppSpec
    tests_dir: str = "tests"

    def to_dict(self) -> dict:
        return {"app": {"module": self.app.module, "port": self.app.port, "health": self.app.health}, "tests_dir": self.tests_dir}


def load_target(data: bytes | str) -> Target:
    """Lee `.contraprueba.json` (el contenido que tiene en la rama base)."""
    d = json.loads(data)
    if not isinstance(d, dict) or not isinstance(d.get("app"), dict):
        raise ValueError(f"{CONFIG_FILE} inválido")
    _check_keys(d, CONFIG_KEYS, CONFIG_FILE)
    _check_keys(d["app"], APP_KEYS, "app")
    return Target(AppSpec(**d["app"]), safe_relpath(d.get("tests_dir", "tests"), "tests_dir"))


@dataclass(frozen=True)
class PRSpec:
    name: str
    kind: str
    description: str = ""
    delete: tuple[str, ...] = ()

    @property
    def is_attack_on_eval(self) -> bool:
        """Etiqueta: el PR intenta alterar la evaluación."""
        return self.kind == "cheat"


@dataclass(frozen=True)
class Case:
    id: str
    root: Path
    app: AppSpec
    tests_dir: str = "tests"
    attacks_file: str | None = None
    # Tests funcionales reservados: viven fuera de base/, el parchador no los ve.
    holdout_tests_dir: str | None = None
    split_shown: float = 0.4
    split_seed: int = 0
    description: str = ""
    prs: tuple[PRSpec, ...] = field(default_factory=tuple)

    @property
    def base_dir(self) -> Path:
        return self.root / "base"

    def overlay_dir(self, pr: PRSpec) -> Path:
        return self.root / "prs" / pr.name

    @property
    def holdout_tests_path(self) -> Path | None:
        return self.root / self.holdout_tests_dir if self.holdout_tests_dir else None

    def target(self) -> Target:
        return Target(self.app, self.tests_dir)

    def attacks(self) -> list[Attack]:
        return load_attacks(self.root / self.attacks_file) if self.attacks_file else []

    def attacks_for(self, pr: str, mode: str) -> list[Attack] | None:
        """Ataques que se ejecutan contra un PR en un modo, o `None` si no corresponde.

        Solo los PR que parchean la falla (`half_patch`, `correct_patch`) se
        evalúan con ataques: en modo `bot` los visibles, en los demás los reservados.
        """
        if self.pr(pr).kind not in ("half_patch", "correct_patch"):
            return None
        visible, reserved = split(self.attacks(), self.split_shown, self.split_seed)
        return visible if mode == "bot" else reserved

    def pr(self, name: str) -> PRSpec:
        for p in self.prs:
            if p.name == name:
                return p
        raise KeyError(name)


def _check_keys(d: dict, allowed: set[str], where: str) -> None:
    unknown = set(d) - allowed
    if unknown:
        raise ValueError(f"claves desconocidas en {where}: {sorted(unknown)}")


def safe_relpath(rel: str, what: str) -> str:
    """Ruta relativa confinada: sin absolutas, sin `..` y sin tocar `.git`."""
    if not isinstance(rel, str) or not rel or "\\" in rel:
        raise ValueError(f"{what}: ruta inválida {rel!r}")
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or ".git" in p.parts or p.parts[0] in ("", "."):
        raise ValueError(f"{what}: ruta fuera del repo o reservada {rel!r}")
    return str(p)


def load_case(path: str | Path) -> Case:
    """Carga `case.json` desde la carpeta del caso o desde el archivo."""
    p = Path(path)
    root = p if p.is_dir() else p.parent
    d = json.loads((root / "case.json").read_text())
    _check_keys(d, CASE_KEYS, "case.json")
    _check_keys(d["app"], APP_KEYS, "app")
    prs = []
    for pd in d.get("prs", []):
        _check_keys(pd, PR_KEYS, f"pr {pd.get('name')}")
        if pd["kind"] not in PR_KINDS:
            raise ValueError(f"tipo de PR desconocido: {pd['kind']}")
        if not BRANCH_RE.match(str(pd["name"])) or pd["name"] == "main":
            raise ValueError(f"nombre de PR inválido: {pd['name']!r}")
        delete = pd.get("delete", [])
        if not isinstance(delete, list):
            raise ValueError("delete debe ser una lista")
        delete = tuple(safe_relpath(r, f"pr {pd['name']}") for r in delete)
        prs.append(PRSpec(pd["name"], pd["kind"], pd.get("description", ""), delete))
    names = [p.name for p in prs]
    if len(names) != len(set(names)):
        raise ValueError("nombres de PR repetidos")
    sp = d.get("split", {})
    _check_keys(sp, SPLIT_KEYS, "split")
    case = Case(
        id=d["id"],
        root=root.resolve(),
        app=AppSpec(**d["app"]),
        tests_dir=safe_relpath(d.get("tests_dir", "tests"), "tests_dir"),
        attacks_file=safe_relpath(d["attacks"], "attacks") if d.get("attacks") else None,
        holdout_tests_dir=safe_relpath(d["holdout_tests"], "holdout_tests") if d.get("holdout_tests") else None,
        split_shown=float(sp.get("shown", 0.4)),
        split_seed=int(sp.get("seed", 0)),
        description=d.get("description", ""),
        prs=tuple(prs),
    )
    if not case.base_dir.is_dir():
        raise ValueError(f"falta {case.base_dir}")
    if not (case.base_dir / case.tests_dir).is_dir():
        raise ValueError(f"falta {case.base_dir / case.tests_dir}")
    if case.holdout_tests_path and not case.holdout_tests_path.is_dir():
        raise ValueError(f"falta {case.holdout_tests_path}")
    for pr in case.prs:
        if not case.overlay_dir(pr).is_dir() and not pr.delete:
            raise ValueError(f"el PR {pr.name} no tiene capa ni borrados")
    return case


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=contraprueba", "-c", "user.email=contraprueba@localhost",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args,
        ],
        check=True,
        capture_output=True,
    )


def _copy_tree(src: Path, dst: Path) -> None:
    """Copia archivos regulares. Rechaza enlaces simbólicos y metadatos de git."""
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src)
        if "__pycache__" in rel.parts:
            continue
        if ".git" in rel.parts:
            raise ValueError(f"la capa no puede traer metadatos de git: {rel}")
        if f.is_symlink():
            raise ValueError(f"la capa no puede traer enlaces simbólicos: {rel}")
        if f.is_dir():
            continue
        if not f.is_file():
            raise ValueError(f"tipo de archivo no soportado: {rel}")
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)


def _confined(repo: Path, rel: str) -> Path:
    target = (repo / rel).resolve()
    if repo.resolve() not in target.parents:
        raise ValueError(f"ruta fuera del repo: {rel}")
    return target


def materialize(case: Case, dest: str | Path) -> Path:
    """Crea en `dest` un repo git con `main` (la base) y una rama por PR.

    La base lleva además `.contraprueba.json`, generado desde `case.json`.

    Los casos son fixtures locales confiables; aun así las capas se validan para
    que no escriban ni borren fuera de `dest` ni toquen `.git`. No es un sandbox.
    """
    repo = Path(dest)
    if repo.exists() and any(repo.iterdir()):
        raise FileExistsError(f"{repo} no está vacío")
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _copy_tree(case.base_dir, repo)
    (repo / CONFIG_FILE).write_text(json.dumps(case.target().to_dict(), indent=2) + "\n")
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-q", "-m", f"{case.id}: rama base")
    for pr in case.prs:
        _git(repo, "checkout", "-q", "-b", pr.name, "main")
        if case.overlay_dir(pr).is_dir():
            _copy_tree(case.overlay_dir(pr), repo)
        for rel in pr.delete:
            _confined(repo, rel).unlink()
        _git(repo, "add", "-A", "-f")
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"{case.id}: {pr.name}")
    _git(repo, "checkout", "-q", "main")
    return repo
