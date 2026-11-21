"""Rutas de la app leídas del código, sin depender del OpenAPI.

El equipo rojo arma sus candidatos con las rutas que la app publica en
`/openapi.json`. Flask, Django y Express no lo traen, y un PR puede sacar una
ruta del OpenAPI (`include_in_schema=False`) justo para que no la ataquen.

Este módulo lee el árbol de código (el del PR para atacar la app del PR, el de
la base para la base) y devuelve las rutas que declara:

- Python: decoradores `@x.route("/p", methods=[...])` y `@x.get/post/put/patch/delete("/p")`
  (Flask, FastAPI, Starlette), con `<int:id>` de Flask pasado a `{id}` y el
  `url_prefix` de un `Blueprint`/`APIRouter(prefix=...)` definido en el mismo
  archivo; parámetros de consulta leídos con `request.args.get("x")`,
  `request.args["x"]` o `request.query_params.get("x")` en la función.
- Django: `path("notas/<int:pk>/", vista)` en `urls.py`.
- Express: `app.get("/p/:id", ...)`, `router.put(...)`, con parámetros de
  consulta `req.query.x` o `req.query["x"]` del mismo archivo.

Es análisis estático: lo que no se escribe literal (rutas armadas en tiempo de
ejecución, prefijos de otro archivo) no se ve. Por eso se une con el OpenAPI
en vez de reemplazarlo.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

METHODS = ("get", "post", "put", "patch", "delete")
MAX_FILE = 1_000_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "tests", "test", ".cachai-wheels"}
FLASK_PARAM = re.compile(r"<(?:[a-zA-Z_]+:)?([a-zA-Z_]\w*)>")
EXPRESS_PARAM = re.compile(r":([a-zA-Z_]\w*)")
EXPRESS_ROUTE = re.compile(r"\b(?:app|router|api)\s*\.\s*(get|post|put|patch|delete)\s*\(\s*(['\"`])([^'\"`]+)\2")
EXPRESS_QUERY = re.compile(r"req\.query(?:\.([a-zA-Z_]\w*)|\[\s*['\"]([^'\"]+)['\"]\s*\])")
PATH_PARAM = re.compile(r"\{([a-zA-Z_]\w*)(?::[^}]*)?\}")


@dataclass
class Route:
    method: str  # GET, POST...
    template: str  # con parámetros como {nombre}
    path_params: list[str]
    query_params: list[str] = field(default_factory=list)
    source: str = ""  # archivo:línea

    def to_dict(self) -> dict:
        return {"method": self.method, "template": self.template, "path_params": self.path_params,
                "query_params": self.query_params, "source": self.source}


def _join(prefix: str, path: str) -> str:
    if not prefix:
        return path if path.startswith("/") else "/" + path
    return "/" + "/".join(p.strip("/") for p in (prefix, path) if p.strip("/"))


def _normalize(template: str) -> tuple[str, list[str]]:
    t = FLASK_PARAM.sub(lambda m: "{" + m.group(1) + "}", template)
    t = EXPRESS_PARAM.sub(lambda m: "{" + m.group(1) + "}", t)
    t = PATH_PARAM.sub(lambda m: "{" + m.group(1) + "}", t)
    if not t.startswith("/"):
        t = "/" + t
    return t, PATH_PARAM.findall(t)


def _str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _query_params(func: ast.AST) -> list[str]:
    """`request.args.get("x")`, `request.args["x"]`, `request.query_params.get("x")` dentro de la función."""
    found: list[str] = []
    for node in ast.walk(func):
        attr = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "getlist") and node.args:
            attr, key = node.func.value, _str(node.args[0])
        elif isinstance(node, ast.Subscript):
            attr, key = node.value, _str(node.slice)
        else:
            continue
        if isinstance(attr, ast.Attribute) and attr.attr in ("args", "query_params") and key and key not in found:
            found.append(key)
    return found


def _python_routes(path: Path, rel: str, source: str) -> list[Route]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []
    # Prefijos: bp = Blueprint("x", __name__, url_prefix="/a") o router = APIRouter(prefix="/a").
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            for kw in node.value.keywords:
                if kw.arg in ("url_prefix", "prefix") and _str(kw.value):
                    prefixes[node.targets[0].id] = _str(kw.value)
    out: list[Route] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.args):
                continue
            name, raw = dec.func.attr, _str(dec.args[0])
            if raw is None:
                continue
            owner = dec.func.value.id if isinstance(dec.func.value, ast.Name) else ""
            if name == "route":
                methods = ["GET"]
                for kw in dec.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        methods = [m.upper() for m in (_str(e) for e in kw.value.elts) if m]
            elif name in METHODS:
                methods = [name.upper()]
            else:
                continue
            template, params = _normalize(_join(prefixes.get(owner, ""), raw))
            query = _query_params(node)
            for m in methods:
                if m.lower() in METHODS:
                    out.append(Route(m, template, params, list(query), f"{rel}:{dec.lineno}"))
    # Django: path("x/<int:pk>/", vista) en urls.py.
    if path.name == "urls.py":
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "path" and node.args:
                raw = _str(node.args[0])
                if raw is not None:
                    template, params = _normalize(raw)
                    out.append(Route("GET", template, params, [], f"{rel}:{node.lineno}"))
    return out


def _express_routes(rel: str, source: str) -> list[Route]:
    query = []
    for m in EXPRESS_QUERY.finditer(source):
        q = m.group(1) or m.group(2)
        if q not in query:
            query.append(q)
    out = []
    for m in EXPRESS_ROUTE.finditer(source):
        template, params = _normalize(m.group(3))
        line = source.count("\n", 0, m.start()) + 1
        out.append(Route(m.group(1).upper(), template, params, list(query), f"{rel}:{line}"))
    return out


def extract(root: Path) -> list[Route]:
    """Rutas declaradas en el código de `root`, sin duplicados (método y plantilla)."""
    found: dict[tuple[str, str], Route] = {}
    for f in sorted(root.rglob("*")):
        rel_parts = f.relative_to(root).parts
        if any(p in SKIP_DIRS for p in rel_parts[:-1]) or f.is_symlink() or not f.is_file():
            continue
        if f.suffix not in (".py", ".js", ".mjs", ".cjs", ".ts") or f.stat().st_size > MAX_FILE:
            continue
        rel = "/".join(rel_parts)
        text = f.read_text(errors="replace")
        routes = _python_routes(f, rel, text) if f.suffix == ".py" else _express_routes(rel, text)
        for r in routes:
            key = (r.method, r.template)
            if key in found:
                found[key].query_params += [q for q in r.query_params if q not in found[key].query_params]
            else:
                found[key] = r
    return list(found.values())


def merge(openapi_routes: list[tuple[str, list[str], list[str]]], static: list[dict]) -> tuple[list, list[dict]]:
    """Une las rutas GET del OpenAPI con las leídas del código.

    Devuelve (rutas GET para el equipo rojo, rutas del código que no están en el OpenAPI).
    """
    by_template = {t: (t, list(pp), list(qp)) for t, pp, qp in openapi_routes}
    hidden = []
    for r in static:
        if r["method"] != "GET":
            continue
        t = r["template"]
        if t in by_template:
            _, pp, qp = by_template[t]
            qp += [q for q in r["query_params"] if q not in qp]
        else:
            by_template[t] = (t, list(r["path_params"]), list(r["query_params"]))
            hidden.append(r)
    return sorted(by_template.values()), hidden
