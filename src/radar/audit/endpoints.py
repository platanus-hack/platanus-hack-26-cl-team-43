"""Lo que cada proveedor declara en OpenRouter (API pública, sin clave). Nivel E1."""
from collections import Counter

import httpx

from radar.audit.client import OPENROUTER, Target

UNDECLARED = (None, "", "unknown")


def list_endpoints(model: str, http: httpx.Client) -> list[dict]:
    resp = http.get(f"{OPENROUTER}/models/{model}/endpoints")
    resp.raise_for_status()
    return resp.json()["data"]["endpoints"]


def _per_million(value) -> float | None:
    try:
        return float(value) * 1_000_000
    except (TypeError, ValueError):
        return None


def to_targets(model: str, endpoints: list[dict], api_key: str) -> list[Target]:
    author = model.split("/", 1)[0]
    out = []
    for e in endpoints:
        tag = e["tag"]
        out.append(Target(
            name=tag, model=model, api_key=api_key, provider_tag=tag,
            quantization=None if e.get("quantization") in UNDECLARED else e["quantization"],
            price_out=_per_million((e.get("pricing") or {}).get("completion")),
            first_party=tag.split("/", 1)[0] == author,
        ))
    return out


def declared_summary(endpoints: list[dict]) -> dict:
    prices = sorted(p for e in endpoints
                    if (p := _per_million((e.get("pricing") or {}).get("completion"))))
    quants = Counter("sin declarar" if e.get("quantization") in UNDECLARED
                     else e["quantization"] for e in endpoints)
    return {
        "endpoints": len(endpoints),
        "sin_cuantizacion_declarada": quants.get("sin declarar", 0),
        "cuantizaciones": dict(quants.most_common()),
        "precio_salida_min": prices[0] if prices else None,
        "precio_salida_max": prices[-1] if prices else None,
        "precio_ratio": round(prices[-1] / prices[0], 1) if prices and prices[0] else None,
    }


def pick(targets: list[Target], n: int, only: list[str] | None = None) -> list[Target]:
    """Los que pidió el usuario, o la referencia first-party más los n más baratos."""
    if only:
        by_name = {t.name: t for t in targets}
        missing = [o for o in only if o not in by_name]
        if missing:
            raise ValueError(f"proveedor desconocido: {', '.join(missing)}")
        return [by_name[o] for o in only]
    first = [t for t in targets if t.first_party][:1]
    rest = sorted((t for t in targets if t not in first),
                  key=lambda t: (t.price_out is None, t.price_out or 0))
    return first + rest[:max(0, n - len(first))]
