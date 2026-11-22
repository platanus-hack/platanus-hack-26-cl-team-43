"""Corre el juego de pruebas contra cada endpoint, con tope de gasto."""
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx

from radar.audit.client import Reply, Target, ask
from radar.audit.suite import Probe

log = logging.getLogger(__name__)


class Spend:
    def __init__(self, cap_usd: float):
        self.cap_usd = cap_usd
        self.spent = 0.0

    def left(self) -> bool:
        return self.spent < self.cap_usd


def _run_target(target: Target, probes: list[Probe], http: httpx.Client, spend: Spend,
                on_reply: Callable[[Reply], None] | None) -> dict[str, Reply]:
    out: dict[str, Reply] = {}
    auth_dead = False
    for p in probes:
        if auth_dead or not spend.left():
            break
        r = ask(target, p.id, p.as_messages(), http=http,
                tools=list(p.tools) if p.tools else None, max_tokens=p.max_tokens)
        spend.spent += r.cost_usd
        out[p.id] = r
        if on_reply:
            on_reply(r)
        # una clave inválida o sin crédito no se arregla reintentando
        auth_dead = r.error_kind == "auth"
    return out


def run(targets: list[Target], probes: list[Probe], *, http: httpx.Client,
        max_usd: float = 2.0, workers: int = 4,
        on_reply: Callable[[Reply], None] | None = None
        ) -> tuple[dict[str, dict[str, Reply]], Spend]:
    spend = Spend(max_usd)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(targets)))) as pool:
        results = pool.map(lambda t: _run_target(t, probes, http, spend, on_reply), targets)
        replies = {t.name: r for t, r in zip(targets, results)}
    if not spend.left():
        log.warning("tope de gasto alcanzado: %.4f USD", spend.spent)
    return replies, spend
