"""Cliente ligero para modelos servidos con la API de OpenAI (`/v1/chat/completions`).

Sirve igual a un Inference Endpoint de Hugging Face que a un servidor multi-LoRA
(vLLM, TGI): lo único que cambia es el identificador de modelo o de adaptador.
Se configura por entorno:

- `CACHAI_LLM_BASE_URL`: raíz de la API, con el `/v1` (sin ella no hay modelo).
- `CACHAI_LLM_API_KEY`: token `Bearer`, opcional.
- `CACHAI_RED_MODEL`: modelo o adaptador del equipo rojo.
- `CACHAI_BLUE_MODEL`: modelo o adaptador del parchador.
- `CACHAI_LLM_TIMEOUT`: segundos por pedido (30 por defecto).

La salida del modelo es un texto no confiable. Se pide con `response_format`
(esquema JSON) y, además, se valida aquí contra el mismo esquema: lo que no
cumple se descarta con `LLMError` antes de que llegue a quien lo usa. Solo se
usa la biblioteca estándar. La clave nunca aparece en errores ni en `repr`.
"""

from __future__ import annotations

import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_TOKENS = 8192  # alcanza para devolver archivos completos en un parche
MAX_RESPONSE = 4_000_000  # bytes; una respuesta más grande se descarta
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class LLMError(RuntimeError):
    """Falla al consultar el modelo o respuesta que no cumple el esquema."""


class LLMHTTPError(LLMError):
    """El servidor respondió con un status HTTP de error (o una redirección no seguida)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"el servidor del modelo respondió HTTP {status}" + (" (redirección no seguida)" if 300 <= status < 400 else ""))
        self.status = status


def warn(message: str) -> None:
    """Aviso en stderr. Nunca recibe la clave: los mensajes de este módulo no la incluyen."""
    print(f"cachai: aviso: {message}", file=sys.stderr)


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str = field(default="", repr=False)
    red_model: str = ""
    blue_model: str = ""
    timeout: float = DEFAULT_TIMEOUT
    max_tokens: int = DEFAULT_MAX_TOKENS

    def __post_init__(self) -> None:
        url = urllib.parse.urlparse(self.base_url)
        if url.scheme not in ("http", "https"):
            raise LLMError("CACHAI_LLM_BASE_URL debe empezar con http:// o https://")
        if self.api_key and url.scheme == "http" and url.hostname not in LOCAL_HOSTS:
            raise LLMError("no se envía CACHAI_LLM_API_KEY por http:// a un servidor remoto: usa https://")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LLMConfig | None":
        """La configuración del entorno, o None si no hay `CACHAI_LLM_BASE_URL` (modo sin red)."""
        if env is None:
            import os
            env = os.environ
        base = env.get("CACHAI_LLM_BASE_URL", "").strip()
        if not base:
            return None
        try:
            timeout = float(env.get("CACHAI_LLM_TIMEOUT", "") or DEFAULT_TIMEOUT)
        except ValueError:
            raise LLMError("CACHAI_LLM_TIMEOUT debe ser un número de segundos") from None
        if timeout <= 0:
            raise LLMError("CACHAI_LLM_TIMEOUT debe ser positivo")
        try:
            max_tokens = int(env.get("CACHAI_LLM_MAX_TOKENS", "") or DEFAULT_MAX_TOKENS)
        except ValueError:
            raise LLMError("CACHAI_LLM_MAX_TOKENS debe ser un entero") from None
        if max_tokens <= 0:
            raise LLMError("CACHAI_LLM_MAX_TOKENS debe ser positivo")
        return cls(base.rstrip("/"), env.get("CACHAI_LLM_API_KEY", ""), env.get("CACHAI_RED_MODEL", ""),
                   env.get("CACHAI_BLUE_MODEL", ""), timeout, max_tokens)


def load_config() -> LLMConfig | None:
    """Como `from_env`, pero una configuración inválida avisa por stderr y deja el modo sin red."""
    try:
        return LLMConfig.from_env()
    except LLMError as e:
        warn(f"configuración del modelo inválida, se usan solo las plantillas: {e}")
        return None


# --- validación mínima contra el esquema -----------------------------------

_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}


def validate_schema(value: object, schema: dict, where: str = "$") -> None:
    """Valida `value` contra el subconjunto de JSON Schema que usan los esquemas de este módulo.

    Cubre `type` (object, array, string, integer, boolean), `enum`, `properties`, `required`,
    `additionalProperties: false`, `items`, `minItems`, `maxItems`, `minLength`
    y `maxLength`. Lanza `LLMError`.
    """
    t = schema.get("type")
    # En Python `bool` es un `int`: solo cuenta como entero si el esquema pide boolean, y al revés.
    if t and not (isinstance(value, _TYPES[t]) and isinstance(value, bool) == (t == "boolean")):
        raise LLMError(f"{where}: se esperaba {t}")
    if "enum" in schema and value not in schema["enum"]:
        raise LLMError(f"{where}: valor fuera de los permitidos")
    if isinstance(value, str) and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
        raise LLMError(f"{where}: largo fuera de rango")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise LLMError(f"{where}: falta {key}")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise LLMError(f"{where}: claves no declaradas {sorted(set(value) - set(props))}")
        for key, sub in props.items():
            if key in value:
                validate_schema(value[key], sub, f"{where}.{key}")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
            raise LLMError(f"{where}: cantidad de elementos fuera de rango")
        if "items" in schema:
            for i, item in enumerate(value):
                validate_schema(item, schema["items"], f"{where}[{i}]")


# --- cliente ---------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """No sigue redirecciones: así `Authorization` nunca viaja a otro destino."""

    def redirect_request(self, *args, **kwargs):
        return None


# Sin proxies: HTTP_PROXY/HTTPS_PROXY del entorno nunca ven el pedido ni la clave.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
_THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _clean(content: str) -> str:
    """Quita un bloque `<think>` inicial y las vallas de código que algunos modelos agregan."""
    content = _THINK.sub("", content, count=1)
    m = _FENCE.match(content)
    return m.group(1) if m else content


def _post(cfg: LLMConfig, body: dict) -> bytes:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    req = urllib.request.Request(cfg.base_url + "/chat/completions", json.dumps(body).encode(), headers, method="POST")
    try:
        with _OPENER.open(req, timeout=cfg.timeout) as resp:
            raw = resp.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as e:
        raise LLMHTTPError(e.code) from None
    except (socket.timeout, TimeoutError):
        raise LLMError("el modelo no respondió a tiempo") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise LLMError("el modelo no respondió a tiempo") from None
        raise LLMError(f"no se pudo llegar al servidor del modelo: {type(e.reason).__name__}") from None
    except OSError as e:
        raise LLMError(f"falla de red al consultar el modelo: {type(e).__name__}") from None
    if len(raw) > MAX_RESPONSE:
        raise LLMError("la respuesta del modelo es demasiado grande")
    return raw


_BOUNDS = {"maxLength", "minLength", "maxItems", "minItems"}


def wire_schema(schema: object) -> object:
    """El esquema que viaja al servidor, sin límites de largo ni de cantidad.

    Los servidores compilan el esquema a una gramática y llama.cpp (Ollama)
    se cae con límites grandes ("number of repetitions exceeds sane defaults").
    Los límites se siguen exigiendo localmente con el esquema completo.
    """
    if isinstance(schema, dict):
        return {k: wire_schema(v) for k, v in schema.items() if k not in _BOUNDS}
    if isinstance(schema, list):
        return [wire_schema(x) for x in schema]
    return schema


def complete_json(cfg: LLMConfig, model: str, system: str, user: str, name: str, schema: dict) -> dict:
    """Pide al modelo una respuesta con la forma de `schema` y la devuelve ya validada.

    Si el servidor rechaza `response_format` con 400 (algunos endpoints no lo
    soportan), se reintenta una vez sin él y el esquema se exige solo aquí.
    Lanza `LLMError` ante cualquier falla: red, tiempo agotado, status HTTP de
    error, redirección, sobre de respuesta inesperado, contenido que no es JSON
    o que no cumple el esquema.
    """
    if not model:
        raise LLMError("no hay modelo configurado (CACHAI_RED_MODEL / CACHAI_BLUE_MODEL)")
    # El esquema va también en el prompt: algunos proveedores (p. ej. Cloudflare en el router de
    # Hugging Face) aceptan response_format pero lo ignoran y devuelven JSON con otra forma.
    system += "\nResponde solo con un JSON que cumpla este esquema: " + json.dumps(wire_schema(schema))
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": cfg.max_tokens,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": name, "strict": True, "schema": wire_schema(schema)}},
    }
    try:
        raw = _post(cfg, body)
    except LLMHTTPError as e:
        if e.status != 400:
            raise
        del body["response_format"]
        raw = _post(cfg, body)
    try:
        content = json.loads(raw)["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError
        data = json.loads(_clean(content))
    except (ValueError, KeyError, IndexError, TypeError, RecursionError):
        raise LLMError("la respuesta del modelo no es JSON con la forma esperada") from None
    validate_schema(data, schema)
    return data


# --- esquemas de salida estructurada ---------------------------------------

MAX_VARIANTS = 20
MAX_PATCH_FILES = 10

VARIANTS_SCHEMA = {
    "type": "object",
    "properties": {
        "variants": {
            "type": "array",
            "maxItems": MAX_VARIANTS,
            "items": {
                "type": "object",
                "properties": {
                    "family": {"type": "string", "enum": ["authz", "files"]},
                    "path": {"type": "string", "minLength": 1, "maxLength": 500},
                    "owner_path": {"type": "string", "minLength": 1, "maxLength": 500},
                    "attacker": {"type": "string", "minLength": 1, "maxLength": 60},
                    "target": {"type": "string", "minLength": 1, "maxLength": 300},
                },
                "required": ["family", "path", "owner_path", "attacker", "target"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["variants"],
    "additionalProperties": False,
}

PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "maxLength": 2000},
        "files": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_PATCH_FILES,
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 300},
                    "content": {"type": "string", "maxLength": 200_000},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "files"],
    "additionalProperties": False,
}
