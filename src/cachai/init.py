"""`cachai init`: deja un repo con una API FastAPI listo para que Cachai revise sus PR.

Pasos:

1. Detecta en el código el objeto ASGI (`X = FastAPI(` o `app = ...(`), el
   puerto y la ruta de salud. Si no hay ruta de salud, propone una y, mientras
   tanto, usa `/openapi.json` (FastAPI la publica sola).
2. Levanta la app del árbol actual en el sandbox (sin red, como en una
   revisión), lee `/openapi.json` y, con los usuarios de prueba que se le dan
   (headers o un comando de login), mira qué ve cada uno en los listados para
   proponer un borrador de inventario: registros con dueño y sus marcas. Los
   adjuntos con dueño salen de carpetas con el nombre de cada usuario.
3. Corre el equipo rojo contra esa misma app (la base) y sella el paquete
   reservado con los ataques confirmados y, si se le pasan, los tests
   funcionales reservados. La clave nunca se escribe a disco salvo con
   `--key-file`: se imprime una vez para guardarla en un secreto del CI.
4. Escribe `.cachai.json` v2 (validado con el cargador y el esquema) y
   `.github/workflows/cachai.yml` (sale de `examples/workflow.yml`), y explica
   cómo marcar el check como obligatorio.

Con `--yes` no pregunta nada: acepta cada propuesta (para agentes y CI).
Quien corre `init` ve el material reservado: debe correrlo quien mantiene el
repo, nunca el agente cuyos PR se van a revisar.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import hidden
from .case import CONFIG_FILE, AppSpec, Hidden, Target, load_target
from .holdout import FAMILIES, Attack, split_record
from .redteam import MIN_MARKER, NAME_RE, Inventory

SKIP_DIRS = {".git", ".venv", "venv", "env", ".env", "node_modules", "__pycache__", "site-packages", ".tox",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", "build", "dist", ".github", ".cachai"}
ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_WORKFLOW = ROOT / "examples" / "workflow.yml"
WORKFLOW_FILE = ".github/workflows/cachai.yml"
DEFAULT_PACKAGE = ".cachai/reservado.cachai"
DEFAULT_KEY_ENV = "CACHAI_HIDDEN_KEY"
FALLBACK_HEALTH = "/openapi.json"  # FastAPI la publica sin que nadie la escriba
CHECK_NAME = "cachai"  # nombre del job del workflow: es el check que se marca como obligatorio

FASTAPI_RE = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*(?:[\w.]+\.)?FastAPI\(", re.M)
APP_ASSIGN_RE = re.compile(r"^(app|application|api)\s*(?::[^=\n]+)?=\s*[A-Za-z_][\w.]*\(", re.M)
HEALTH_NAMES = ("/health", "/healthz", "/healthcheck", "/livez", "/readyz", "/ready", "/ping", "/status", "/_health")
ROUTE_RE = re.compile(r"@\w+\.(?:get|head|api_route)\(\s*[\"'](/[^\"']*)[\"']")
PORT_PATTERNS = (
    re.compile(r"--port[= ](\d{2,5})"),
    re.compile(r"uvicorn\.run\([^)]*port\s*=\s*(\d{2,5})"),
    re.compile(r"^EXPOSE\s+(\d{2,5})", re.M),
    re.compile(r"^PORT\s*=\s*(\d{2,5})\s*$", re.M),
)
HEALTH_SNIPPET = '''

@{obj}.get("/health")
def health() -> dict:
    return {{"ok": True}}
'''


class InitError(RuntimeError):
    pass


# --- Detección -------------------------------------------------------------

def py_files(root: Path, include_tests: bool = False) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                             and (include_tests or d not in ("tests", "test")))
        for f in sorted(filenames):
            if f.endswith(".py") and (include_tests or not (f.startswith("test_") or f == "conftest.py")):
                out.append(Path(dirpath) / f)
    return out


def module_of(root: Path, file: Path) -> str:
    parts = list(file.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@dataclass(frozen=True)
class AsgiCandidate:
    module: str  # p. ej. invoices.main:app
    file: Path
    fastapi: bool  # `FastAPI(` explícito, no solo `app = algo(`


def detect_asgi(root: Path) -> list[AsgiCandidate]:
    """Objetos ASGI candidatos, el más probable primero: `FastAPI(` explícito,
    luego un `app = ...(` cualquiera; a igualdad, el módulo menos anidado."""
    found: list[AsgiCandidate] = []
    for f in py_files(root):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        mod = module_of(root, f)
        if not mod or not all(p.isidentifier() for p in mod.split(".")):
            continue
        names = [(m.group(1), True) for m in FASTAPI_RE.finditer(text)]
        names += [(m.group(1), False) for m in APP_ASSIGN_RE.finditer(text) if m.group(1) not in {n for n, _ in names}]
        found += [AsgiCandidate(f"{mod}:{n}", f, fa) for n, fa in names]
    return sorted(found, key=lambda c: (not c.fastapi, c.module.count("."), c.module.split(":")[1] != "app", c.module))


def get_routes_in_code(root: Path) -> list[str]:
    routes: list[str] = []
    for f in py_files(root):
        try:
            routes += ROUTE_RE.findall(f.read_text(errors="replace"))
        except OSError:
            continue
    return routes


def detect_health(root: Path) -> str | None:
    routes = set(get_routes_in_code(root))
    return next((h for h in HEALTH_NAMES if h in routes), None)


def detect_port(root: Path) -> int | None:
    files = [root / n for n in ("Dockerfile", "Procfile", ".env.example", "docker-compose.yml", "compose.yaml",
                                "docker-compose.yaml", "compose.yml") if (root / n).is_file()]
    for f in files + py_files(root):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for pat in PORT_PATTERNS:
            m = pat.search(text)
            if m and 1 <= int(m.group(1)) <= 65535:
                return int(m.group(1))
    return None


def detect_tests_dir(root: Path) -> str:
    return next((d for d in ("tests", "test") if (root / d).is_dir()), "tests")


@dataclass
class Detected:
    candidates: list[AsgiCandidate]
    health: str | None
    port: int | None
    tests_dir: str
    dockerfile: str | None
    compose: str | None

    @property
    def module(self) -> str | None:
        return self.candidates[0].module if self.candidates else None


def detect(root: Path) -> Detected:
    compose = next((n for n in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml") if (root / n).is_file()), None)
    return Detected(detect_asgi(root), detect_health(root), detect_port(root), detect_tests_dir(root),
                    "Dockerfile" if (root / "Dockerfile").is_file() else None, compose)


# --- Usuarios de prueba -------------------------------------------------------

def parse_header(line: str) -> tuple[str, str]:
    name, sep, value = line.partition(":")
    name, value = name.strip(), value.strip()
    if not sep or not name or not value or not re.fullmatch(r"[A-Za-z0-9-]+", name):
        raise InitError(f"header inválido {line!r}: se espera «Nombre: valor»")
    return name, value


def parse_user(spec: str) -> tuple[str, str]:
    """`alice=Authorization: Bearer tok` -> ("alice", "Authorization: Bearer tok")."""
    name, sep, rest = spec.partition("=")
    name = name.strip()
    if not sep or not NAME_RE.match(name):
        raise InitError(f"usuario inválido {spec!r}: se espera nombre=«Header: valor» (nombre en minúsculas)")
    return name, rest.strip()


def login_header(command: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> tuple[str, str]:
    """Corre el comando de login (en esta máquina, sin shell) y toma la última línea:
    un header `Nombre: valor`, o un token suelto que va como `Authorization: Bearer`."""
    proc = runner(shlex.split(command), capture_output=True, text=True, timeout=60, check=False)
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        raise InitError(f"el comando de login falló ({proc.returncode}): {command}")
    last = lines[-1]
    return parse_header(last) if re.match(r"^[A-Za-z0-9-]+:\s", last) else ("Authorization", f"Bearer {last}")


def collect_users(user_specs: list[str], login_specs: list[str],
                  runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict[str, dict[str, str]]:
    users: dict[str, dict[str, str]] = {}
    for spec in user_specs:
        name, header = parse_user(spec)
        k, v = parse_header(header)
        users.setdefault(name, {})[k] = v
    for spec in login_specs:
        name, command = parse_user(spec)
        k, v = login_header(command, runner)
        users.setdefault(name, {})[k] = v
    return users


# --- Borrador de inventario ---------------------------------------------------

def _items(body: object) -> list[dict]:
    if isinstance(body, dict):
        for key in ("items", "data", "results"):
            if isinstance(body.get(key), list):
                body = body[key]
                break
    return [o for o in body if isinstance(o, dict)] if isinstance(body, list) else []


def id_routes(openapi: dict) -> list[str]:
    """Rutas GET que reciben un id (por la ruta o la consulta): las que el equipo rojo prueba."""
    out = []
    for template, item in sorted((openapi.get("paths") or {}).items()):
        op = item.get("get") if isinstance(item, dict) else None
        if isinstance(op, dict) and any(isinstance(p, dict) and p.get("in") in ("path", "query") for p in op.get("parameters", [])):
            out.append(template)
    return out


def draft_records(samples: dict[str, dict[str, object]], users: dict[str, dict[str, str]]) -> list[dict]:
    """Registros con dueño a partir de lo que cada usuario ve en los listados.

    Un objeto con `id` que solo aparece en los listados de un usuario es suyo.
    Sus marcas son textos del objeto (de 4 caracteres o más) que no aparecen en
    nada de lo que ven los demás ni en otro registro: si un atacante los recibe,
    vio datos ajenos.
    """
    owned: dict[str, dict[str, dict]] = {u: {} for u in users}
    seen_by: dict[str, set[str]] = {}
    for _, by_user in sorted(samples.items()):
        for user, body in by_user.items():
            if user not in owned:
                continue
            for obj in _items(body):
                rid = obj.get("id")
                if isinstance(rid, bool) or not isinstance(rid, (int, str)) or str(rid) == "":
                    continue
                owned[user].setdefault(str(rid), obj)
                seen_by.setdefault(str(rid), set()).add(user)
    raw = {u: json.dumps([b for by in samples.values() for usr, b in by.items() if usr == u], ensure_ascii=False) for u in users}
    values: dict[str, int] = {}
    for u in owned:
        for obj in owned[u].values():
            for v in {v for v in obj.values() if isinstance(v, str)}:
                values[v] = values.get(v, 0) + 1
    records = []
    for user in sorted(owned):
        for rid, obj in sorted(owned[user].items(), key=lambda kv: (len(kv[0]), kv[0])):
            if len(seen_by[rid]) > 1:
                continue  # lo ven varios: no tiene un dueño claro
            markers = [v for k, v in obj.items() if isinstance(v, str) and len(v) >= MIN_MARKER and k != "id"
                       and values.get(v) == 1 and v not in users
                       and not any(v in raw[o] for o in users if o != user)]
            if markers:
                records.append({"id": rid, "owner": user, "markers": markers[:2]})
    return records


def draft_files(root: Path, users: dict[str, dict[str, str]], taken: set[str], app_root: str = "/app") -> tuple[list[dict], str]:
    """Adjuntos con dueño: una carpeta del repo con una subcarpeta por usuario de prueba.

    La marca de cada archivo es su primera línea no vacía, si no aparece en otro.
    `files_root` es esa carpeta dentro del contenedor (la imagen de la app copia el repo a /app).
    """
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        here = Path(dirpath)
        mine = [u for u in sorted(users) if (here / u).is_dir()]
        if len(mine) < 2:
            continue
        files, firsts = [], []
        for u in mine:
            for f in sorted((here / u).rglob("*")):
                if not f.is_file() or f.is_symlink() or f.stat().st_size > 65536:
                    continue
                try:
                    first = next((ln.strip() for ln in f.read_text().splitlines() if ln.strip()), "")
                except (UnicodeDecodeError, OSError):
                    continue
                if len(first) >= MIN_MARKER:
                    files.append({"path": f.relative_to(here).as_posix(), "owner": u, "markers": [first[:80]]})
                    firsts.append(first[:80])
        files = [f for f in files if firsts.count(f["markers"][0]) == 1 and f["markers"][0] not in taken]
        if files:
            rel = here.relative_to(root).as_posix()
            return files, f"{app_root.rstrip('/')}/{rel}" if rel != "." else app_root
    return [], ""


def draft_inventory(root: Path, users: dict[str, dict[str, str]], samples: dict) -> dict | None:
    if len(users) < 2:
        return None
    inv: dict = {"users": users}
    records = draft_records(samples, users)
    if records:
        inv["records"] = records
    taken = {m for r in records for m in r["markers"]}
    files, files_root = draft_files(root, users, taken)
    if files:
        inv["files"], inv["files_root"] = files, files_root
    if not records and not files:
        return None
    Inventory.from_dict(inv)  # el mismo control que hace el cargador
    return inv


def default_families(inventory: dict | None) -> list[str]:
    if not inventory:
        return []
    fams = set()
    if inventory.get("records"):
        # `writes`: un PR puede agregar una ruta de escritura sin control de dueño.
        fams |= {"authz", "authn", "sqli", "writes"}
    if inventory.get("files"):
        fams |= {"files", "authn"}
    return [f for f in FAMILIES if f in fams]


# --- Escritura -------------------------------------------------------------

def validate_config(d: dict) -> Target:
    """Valida con el cargador de Cachai y, si está instalado, con el esquema publicado."""
    target = load_target(json.dumps(d))
    try:
        import jsonschema
    except ImportError:
        return target
    schema = json.loads((ROOT / "schema" / "cachai.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(d)
    return target


def workflow_text(key_env: str = DEFAULT_KEY_ENV, example: Path = EXAMPLE_WORKFLOW) -> str:
    """El workflow de `examples/workflow.yml`, con permisos para publicar el
    comentario y el check del PR y el secreto que nombra `hidden.key_env`."""
    if not example.is_file():
        raise InitError(f"no encuentro {example}: instala Cachai desde el repo (pip install -e)")
    lines = example.read_text().splitlines()
    while lines and lines[0].startswith("#"):
        lines.pop(0)
    text = "\n".join(lines) + "\n"
    head = ("# Generado por `cachai init` a partir de examples/workflow.yml.\n"
            f"# Marca el check «{CHECK_NAME}» como obligatorio en la regla de la rama principal.\n")
    perms = "permissions:\n  contents: read\n"
    if perms not in text:
        raise InitError("examples/workflow.yml cambió de forma: no encuentro su bloque de permisos")
    text = text.replace(perms, "permissions:\n  contents: read\n  pull-requests: write  # comentario con el veredicto\n"
                              "  checks: write  # check con el detalle\n")
    if key_env != DEFAULT_KEY_ENV:
        old = "hidden-key: ${{ secrets.CACHAI_HIDDEN_KEY }}"
        if old not in text:
            raise InitError("examples/workflow.yml cambió de forma: no encuentro hidden-key")
        indent = re.search(r"^( *)hidden-key:", text, re.M).group(1)
        text = text.replace(old, f"hidden-key: ${{{{ secrets.{key_env} }}}}\n{indent}hidden-key-env: {key_env}")
    return head + text


def required_check_help(key_env: str | None) -> str:
    secret = f"  1. El secreto con la clave del paquete:  gh secret set {key_env}\n" if key_env else ""
    return f"""Falta configurar en GitHub:
{secret}  {"2" if key_env else "1"}. El check obligatorio. En Settings > Rules > Rulesets (o Branches > Branch protection),
     exige el check «{CHECK_NAME}» en la rama principal. Con gh:
       gh api repos/{{owner}}/{{repo}}/rulesets -X POST --input - <<'JSON'
       {{"name": "cachai", "target": "branch", "enforcement": "active",
        "conditions": {{"ref_name": {{"include": ["~DEFAULT_BRANCH"], "exclude": []}}}},
        "rules": [{{"type": "required_status_checks", "parameters": {{
          "strict_required_status_checks_policy": false,
          "required_status_checks": [{{"context": "{CHECK_NAME}"}}]}}}}]}}
       JSON
     Sin eso, un PR que edita el workflow para no correr Cachai igual se puede mergear."""


# --- Interacción -----------------------------------------------------------

@dataclass
class Prompter:
    """Pregunta en la terminal; con `yes` acepta cada propuesta sin llamar a `input`."""

    yes: bool = False
    input_fn: Callable[[str], str] = input
    out: Callable[[str], None] = field(default=lambda s: print(s, flush=True))

    def ask(self, question: str, default: str = "") -> str:
        if self.yes:
            return default
        answer = self.input_fn(f"{question}{f' [{default}]' if default else ''}: ").strip()
        return answer or default

    def confirm(self, question: str, default: bool = True) -> bool:
        if self.yes:
            return default
        answer = self.input_fn(f"{question} [{'S/n' if default else 's/N'}]: ").strip().lower()
        return default if not answer else answer in ("s", "si", "sí", "y", "yes")


# --- Sandbox ---------------------------------------------------------------

def copy_app_tree(root: Path, dest: Path) -> Path:
    """Copia el árbol actual sin .git ni entornos virtuales: es el contexto de la imagen de la app."""
    def ignore(d: str, names: list[str]) -> set[str]:
        return {n for n in names if n in SKIP_DIRS or (Path(d) / n).is_symlink()}
    shutil.copytree(root, dest, ignore=ignore)
    return dest


@dataclass
class SandboxFindings:
    openapi: dict
    samples: dict
    attacks: list[Attack]
    redteam_error: str = ""


def run_in_sandbox(root: Path, target: Target, users: dict, families: Callable[[dict | None], tuple[dict | None, list[str]]],
                   budget: float, redteam: bool, say: Callable[[str], None]) -> tuple[SandboxFindings, dict | None, list[str]]:
    """Levanta la app una vez: lee el OpenAPI, propone el inventario y corre el equipo rojo."""
    from . import sandbox

    sandbox.build_evaluator_image()
    with tempfile.TemporaryDirectory(prefix="cachai-init-") as tmp:
        tmp_path = Path(tmp)
        context = copy_app_tree(root, tmp_path / "app")
        say("Levantando la app en el sandbox…")
        app = sandbox.start_app(context, target)
        try:
            users_file = tmp_path / "users.json"
            users_file.write_text(json.dumps({"users": users}))
            data = sandbox.run_probe(app, target, users_file, budget)
            inventory, fams = families(data)
            attacks: list[Attack] = []
            error = ""
            if redteam and inventory and fams:
                say(f"Equipo rojo contra la base ({', '.join(fams)})…")
                inv_file = tmp_path / "inventory.json"
                inv_file.write_text(json.dumps(inventory, ensure_ascii=False))
                try:
                    attacks = [Attack.from_dict(d) for d in sandbox.run_redteam(app, target, inv_file, budget, tuple(fams))]
                except sandbox.SandboxError as e:
                    error = str(e)
        finally:
            sandbox.stop(app)
    return SandboxFindings(data["openapi"], data.get("samples") or {}, attacks, error), inventory, fams


# --- Orquestación ----------------------------------------------------------

@dataclass
class InitOptions:
    repo: Path
    yes: bool = False
    force: bool = False
    module: str | None = None
    port: int | None = None
    health: str | None = None
    tests_dir: str | None = None
    users: list[str] = field(default_factory=list)
    logins: list[str] = field(default_factory=list)
    families: list[str] | None = None
    holdout_tests: Path | None = None
    package: str = DEFAULT_PACKAGE
    key_env: str = DEFAULT_KEY_ENV
    key_file: Path | None = None
    shown: float = 0.4
    seed: int = 0
    budget: float = 60.0
    sandbox: bool = True
    redteam: bool = True
    add_health: bool = False
    workflow: bool = True


@dataclass
class InitResult:
    config: dict
    attacks: int = 0
    groups: int = 0
    sealed: bool = False
    key_generated: bool = False
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0


def run(opts: InitOptions, prompt: Prompter | None = None, err: Callable[[str], None] | None = None) -> InitResult:
    started = time.monotonic()
    p = prompt or Prompter(opts.yes)
    say = p.out
    warn = err or (lambda s: print(s, file=sys.stderr, flush=True))
    root = opts.repo.resolve()
    notes: list[str] = []
    if not root.is_dir():
        raise InitError(f"{root} no es una carpeta")
    if (root / CONFIG_FILE).exists() and not opts.force:
        raise InitError(f"{CONFIG_FILE} ya existe; usa --force para reemplazarlo")

    det = detect(root)
    module = opts.module or det.module
    if det.candidates and not opts.module:
        others = ", ".join(c.module for c in det.candidates[1:4])
        say(f"Objeto ASGI: {det.module}" + (f" (otros candidatos: {others})" if others else ""))
    module = p.ask("Objeto ASGI (módulo:objeto)", module or "")
    if not module:
        raise InitError("no encontré el objeto ASGI (busqué `FastAPI(` y `app =`); pásalo con --module paquete.modulo:app")
    port = opts.port or det.port or 8000
    port = int(p.ask("Puerto", str(port)))
    health = opts.health or det.health
    if health is None:
        cand = next((c for c in det.candidates if c.module == module), None)
        obj = module.split(":")[1]
        snippet = HEALTH_SNIPPET.format(obj=obj)
        say(f"No encontré una ruta de salud. Te propongo agregar a {cand.file.relative_to(root) if cand else 'tu app'}:{snippet}")
        if cand is not None and (opts.add_health or (not opts.yes and p.confirm("¿La agrego ahora?", False))):
            with cand.file.open("a") as fh:
                fh.write(snippet)
            health = "/health"
            notes.append(f"agregué GET /health a {cand.file.relative_to(root)}")
        else:
            health = FALLBACK_HEALTH
            notes.append(f"sin ruta de salud: uso {FALLBACK_HEALTH} hasta que agregues /health")
    else:
        say(f"Ruta de salud: {health}")
    health = p.ask("Ruta de salud", health)
    tests_dir = opts.tests_dir or det.tests_dir
    if not (root / tests_dir).is_dir():
        notes.append(f"no hay carpeta {tests_dir}/: la revisión necesita tests de la base que hablen por HTTP")
    dockerfile = None
    if det.dockerfile and p.confirm(f"Hay un {det.dockerfile}: ¿lo uso para construir la app?", False):
        dockerfile = det.dockerfile
    if det.compose:
        notes.append(f"hay {det.compose}: si la app necesita sus servicios, agrega app.compose a {CONFIG_FILE} a mano")
    app = AppSpec(module=module, port=port, health=health, dockerfile=dockerfile)

    users = collect_users(opts.users, opts.logins)
    if not users and not opts.yes:
        say("Usuarios de prueba: nombre=«Header: valor» (p. ej. alice=Authorization: Bearer tok-alice), "
            "o nombre=!comando de login. Vacío para terminar.")
        while True:
            line = p.ask("Usuario")
            if not line:
                break
            name, rest = parse_user(line)
            if rest.startswith("!"):
                k, v = login_header(rest[1:])
            else:
                k, v = parse_header(rest)
            users.setdefault(name, {})[k] = v
    if len(users) == 1:
        raise InitError("el equipo rojo necesita al menos dos usuarios de prueba (o ninguno)")

    target = Target(app, tests_dir)
    inventory: dict | None = None
    families: list[str] = []
    attacks: list[Attack] = []

    def choose(data: dict) -> tuple[dict | None, list[str]]:
        nonlocal inventory, families
        routes = id_routes(data["openapi"])
        say(f"OpenAPI: {len(data['openapi'].get('paths') or {})} rutas, {len(routes)} con id: {', '.join(routes[:8])}"
            + (" …" if len(routes) > 8 else ""))
        inventory = draft_inventory(root, users, data.get("samples") or {}) if users else None
        if inventory is None:
            if users:
                notes.append("no encontré recursos con dueño en los listados: sin inventario no hay equipo rojo")
            return None, []
        say("Borrador de inventario:\n" + json.dumps(inventory, ensure_ascii=False, indent=2))
        if not p.confirm("¿Uso este inventario?", True):
            inventory = None
            return None, []
        families = list(opts.families) if opts.families is not None else default_families(inventory)
        families = [f for f in p.ask("Familias de ataque", ",".join(families)).split(",") if f]
        return inventory, families

    if opts.sandbox:
        try:
            findings, inventory, families = run_in_sandbox(root, target, users, choose, opts.budget, opts.redteam, say)
        except Exception as e:  # noqa: BLE001 — el mensaje del sandbox es lo que el dev necesita ver
            raise InitError(f"la app no se pudo levantar o leer en el sandbox: {e}") from e
        attacks = findings.attacks
        if findings.redteam_error:
            notes.append(f"el equipo rojo no terminó: {findings.redteam_error}")
    else:
        notes.append("sin sandbox (--no-sandbox): no hay inventario ni equipo rojo")

    groups = sorted({a.group_key for a in attacks})
    if attacks:
        say(f"El equipo rojo confirmó {len(attacks)} ataques contra la base, en {len(groups)} grupos: "
            "la base ya tiene esas fallas. Un PR que no las toca no se bloquea por ellas; uno que intenta "
            "arreglarlas debe cerrarlas todas, también las reservadas.")
    if attacks and len(groups) < 2:
        notes.append("un solo grupo de ataques: no se puede repartir entre visibles y reservados, no se sellan")
        attacks = []

    holdout = opts.holdout_tests
    if holdout is None and not opts.yes:
        answer = p.ask("Carpeta con tests funcionales reservados (fuera del repo; vacío si no hay)")
        holdout = Path(answer).expanduser() if answer else None
    if holdout is not None:
        holdout = holdout.resolve()
        if not holdout.is_dir():
            raise InitError(f"{holdout} no es una carpeta")
        if root == holdout or root in holdout.parents:
            notes.append(f"{holdout} está dentro del repo: el autor del PR la ve; muévela afuera después de sellar")

    hidden_cfg = None
    key_generated = False
    if attacks or holdout is not None:
        key = os.environ.get(opts.key_env)
        if not key:
            key, key_generated = hidden.keygen(), True
        with tempfile.TemporaryDirectory(prefix="cachai-init-") as tmp:
            af = None
            if attacks:
                af = Path(tmp) / "attacks.json"
                af.write_text(json.dumps([a.to_dict() for a in attacks], ensure_ascii=False))
            sealed = hidden.seal(key, af, holdout, opts.shown, opts.seed)
        pkg = Path(opts.package) if opts.package.startswith("/") else root / opts.package
        pkg.parent.mkdir(parents=True, exist_ok=True)
        pkg.write_bytes(sealed)
        hidden_cfg = Hidden(opts.package, opts.key_env)
        split = split_record(attacks, opts.shown, opts.seed) if attacks else None
        say(f"Paquete reservado sellado en {opts.package}"
            + (f": {len(split.visible)} ataques visibles, {len(split.reserved)} reservados" if split else "")
            + (", con tests reservados" if holdout else "") + ".")
        if key_generated:
            if opts.key_file:
                opts.key_file.touch(mode=0o600)
                opts.key_file.write_text(key + "\n")
                say(f"Clave nueva escrita en {opts.key_file} (0600). Pásala al secreto {opts.key_env} y bórrala.")
            else:
                # Una sola vez y por stderr: no queda en disco ni en la salida que se guarda.
                warn(f"Clave nueva del paquete (no se guarda en ningún lado; cópiala ahora al secreto {opts.key_env}):\n{key}")
        else:
            say(f"Usé la clave de {opts.key_env}.")

    cfg_target = Target(app, tests_dir, inventory, tuple(families) if inventory else (), hidden_cfg)
    config = cfg_target.to_dict()
    validate_config(config)
    (root / CONFIG_FILE).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    say(f"Escribí {CONFIG_FILE}.")
    if opts.workflow:
        wf = root / WORKFLOW_FILE
        if wf.exists() and not opts.force:
            notes.append(f"{WORKFLOW_FILE} ya existe; no lo toqué (usa --force)")
        else:
            wf.parent.mkdir(parents=True, exist_ok=True)
            wf.write_text(workflow_text(opts.key_env))
            say(f"Escribí {WORKFLOW_FILE}.")
    say(required_check_help(opts.key_env if hidden_cfg else None))
    for n in notes:
        say(f"nota: {n}")
    return InitResult(config, len(attacks), len(groups) if attacks else 0, hidden_cfg is not None, key_generated, notes,
                      time.monotonic() - started)
