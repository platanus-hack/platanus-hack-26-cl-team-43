"""Marcas canario por corrida.

Las marcas del inventario dicen qué texto prueba que un usuario vio datos de
otro. Si fueran fijas, un PR podría conocerlas y esquivarlas (filtrar justo ese
texto de las respuestas). Con `{{canary:nombre}}` en el inventario, Cachai
genera un valor aleatorio nuevo en cada corrida, se lo pasa a la semilla como
`CACHAI_CANARY_<NOMBRE>` y usa ese mismo valor al atacar.
"""

from __future__ import annotations

import copy
import re
import secrets

PLACEHOLDER = re.compile(r"\{\{canary:([a-z0-9_]{1,30})\}\}")
ENV_PREFIX = "CACHAI_CANARY_"


def names(value: object) -> set[str]:
    """Nombres de marcas canario que aparecen en cualquier texto de `value`."""
    if isinstance(value, str):
        return set(PLACEHOLDER.findall(value))
    if isinstance(value, dict):
        return set().union(*(names(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(names(v) for v in value)) if value else set()
    return set()


def generate(wanted: set[str]) -> dict[str, str]:
    """Un valor aleatorio por nombre. Prefijo legible y 12 hexadecimales: no aparece por casualidad."""
    return {n: f"CNR-{n.upper()}-{secrets.token_hex(6)}" for n in sorted(wanted)}


def resolve(value: object, values: dict[str, str]) -> object:
    """Copia de `value` con cada `{{canary:nombre}}` reemplazado por su valor."""
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            if m.group(1) not in values:
                raise ValueError(f"marca canario sin valor: {m.group(1)}")
            return values[m.group(1)]
        return PLACEHOLDER.sub(sub, value)
    if isinstance(value, dict):
        return {k: resolve(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, values) for v in value]
    return copy.deepcopy(value)


def env(values: dict[str, str]) -> dict[str, str]:
    return {ENV_PREFIX + n.upper(): v for n, v in values.items()}
