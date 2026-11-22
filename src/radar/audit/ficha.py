"""La ficha: qué está declarado (E1), qué está medido (E2) y qué no se puede afirmar."""
import hashlib
import json
import re
from datetime import datetime, timezone

from radar.audit.checks import ALPHA, Card
from radar.audit.client import Reply, Target

LIMITS = [
    "Todo lo medido es nivel E2 (caja negra). «Sin desviación detectada» no es «es el mismo modelo».",
    "No se certifica privacidad: desde afuera no se puede probar que un proveedor no mire ni guarde datos.",
    "«Distinguible de la referencia» no es una acusación: puede ser plantilla, parser o cuantización.",
    "La referencia también es un endpoint: si está cuantizada, se mide equivalencia con ella, no con los pesos.",
    "Una sustitución en una fracción chica del tráfico puede no verse con esta muestra.",
]


def evidence_hash(replies: dict[str, dict[str, Reply]]) -> str:
    rows = [r.to_dict() for per in replies.values() for r in per.values()]
    rows.sort(key=lambda r: (r["target"], r["probe"]))
    blob = json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()


def build(model: str, targets: list[Target], cards: list[Card],
          replies: dict[str, dict[str, Reply]], *, reference: str, suite: str,
          seed: int | None, declared: dict | None, spent_usd: float,
          now: datetime | None = None) -> dict:
    by_name = {t.name: t for t in targets}
    return {
        "modelo": model,
        "fecha_utc": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "juego": suite,
        "semilla": seed,
        "referencia": reference,
        "alpha": ALPHA,
        "gasto_usd": round(spent_usd, 6),
        "declarado_en_openrouter": declared,
        "evidencia_sha256": evidence_hash(replies),
        "endpoints": [
            {**c.to_dict(),
             "cuantizacion_declarada": by_name[c.target].quantization,
             "precio_salida_usd_millon": by_name[c.target].price_out,
             "via": "openrouter" if by_name[c.target].via_openrouter else "api directa"}
            for c in cards],
        "limites": LIMITS,
    }


def _pct(k: int, n: int) -> str:
    return "sin datos" if n == 0 else f"{k}/{n}"


def _row(e: dict) -> str:
    if e["is_reference"]:
        verdict = "referencia"
    elif e["errors"] == e["n"]:
        verdict = "sin respuesta"
    else:
        verdict = "DISTINGUIBLE" if e["distinguible"] else "sin desviación detectada"
    return (f"| `{e['target']}` | {e['cuantizacion_declarada'] or 'sin declarar'} "
            f"| {e['token_verdict']} | {_pct(e['tasks_ok'], e['tasks_n'])} "
            f"| {_pct(e['agree'], e['agree_n'])} | {_pct(e['tools_ok'], e['tools_n'])} "
            f"| {_pct(e['canary_leaks'], e['canary_n'])} | {e['errors']}/{e['n']} "
            f"| {verdict} |")


def to_markdown(f: dict) -> str:
    suite = "ciego" if f["semilla"] is not None else "público (prompts fijos)"
    lines = [
        f"# Ficha de endpoints: `{f['modelo']}`",
        "",
        f"- **Medido:** {f['fecha_utc']} · juego {suite}"
        + (f" · semilla `{f['semilla']}`" if f["semilla"] is not None else ""),
        f"- **Referencia:** `{f['referencia']}` · umbral fijado antes de medir: p < {f['alpha']}",
        f"- **Gasto de la auditoría:** USD {f['gasto_usd']:.4f}",
        f"- **Evidencia (SHA-256 de las respuestas crudas):** `{f['evidencia_sha256']}`",
        "",
    ]
    d = f.get("declarado_en_openrouter")
    if d:
        lines += [
            "## Declarado por los proveedores (E1)",
            "",
            f"- {d['endpoints']} endpoints sirven este modelo; "
            f"{d['sin_cuantizacion_declarada']} no declaran cuantización.",
            f"- Precio de salida: de USD {d['precio_salida_min']:.2f} a "
            f"{d['precio_salida_max']:.2f} por millón de tokens ({d['precio_ratio']}×).",
            "",
        ]
    lines += [
        "## Medido desde afuera (E2)",
        "",
        "| Endpoint | Cuantización (E1) | Tokens de prompt | Tareas correctas | Igual a la referencia "
        "| Tool calls válidas | Filtra el secreto | Errores | Resultado |",
        "|---|---|---|---|---|---|---|---|---|",
        *[_row(e) for e in f["endpoints"]],
        "",
    ]
    flagged = [e for e in f["endpoints"] if e["flags"]]
    if flagged:
        lines += ["## Por qué se distinguen", ""]
        for e in flagged:
            lines += [f"- `{e['target']}`: " + "; ".join(e["flags"]) + "."]
        lines.append("")
    lines += ["## Qué no afirma esta ficha", "", *[f"- {x}" for x in f["limites"]], "",
              "## Cómo volver a correrla", "",
              "```bash",
              f"cachai scan {f['modelo']}"
              + (f" --seed {f['semilla']}" if f["semilla"] is not None else " --public"),
              "```", ""]
    return "\n".join(lines)


def slug(f: dict) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", f"ficha-{f['modelo']}-{f['fecha_utc']}".lower())
    return base.strip("-")


def summary(f: dict) -> str:
    audited = [e for e in f["endpoints"] if not e["is_reference"]]
    n = sum(e["distinguible"] for e in audited)
    return (f"{len(audited)} endpoints de {f['modelo']} medidos contra {f['referencia']}: "
            f"{n} distinguibles de la referencia. Evidencia {f['evidencia_sha256'][:12]}.")
