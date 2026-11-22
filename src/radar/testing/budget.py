"""Daily spend cap and per-provider rate limiting for model calls."""
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

SANTIAGO = ZoneInfo("America/Santiago")


class BudgetExhausted(Exception):
    """Raised before a model call when the daily cap has been reached."""


def santiago_today(now: datetime | None = None) -> date:
    return (now or datetime.now(SANTIAGO)).astimezone(SANTIAGO).date()


def _santiago_midnight(now: datetime | None = None) -> datetime:
    return datetime.combine(santiago_today(now), datetime.min.time(), tzinfo=SANTIAGO)


def spent_today(conn, now: datetime | None = None) -> float:
    """USD spent since 00:00 America/Santiago: results plus baseline calls."""
    row = conn.execute(
        "select coalesce((select sum(cost_usd) from results where created_at >= %s), 0)"
        " + coalesce((select sum(cost_usd) from baselines where date = %s), 0) as spent",
        (_santiago_midnight(now), santiago_today(now)),
    ).fetchone()
    return float(row["spent"])


def can_spend(conn, cap_usd: float) -> bool:
    return spent_today(conn) < cap_usd


def check_budget(conn, cap_usd: float | None) -> None:
    if cap_usd is not None and not can_spend(conn, cap_usd):
        raise BudgetExhausted()


class ProviderRateLimiter:
    """Sliding 60 s window per provider (the openrouter_id prefix before '/')."""

    WINDOW_S = 60.0

    def __init__(self, max_per_minute: int = 30, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.max_per_minute = max_per_minute
        self._clock = clock
        self._sleep = sleep
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def acquire(self, openrouter_id: str) -> None:
        provider = openrouter_id.split("/", 1)[0]
        while True:
            with self._lock:
                now = self._clock()
                calls = self._calls[provider]
                while calls and calls[0] <= now - self.WINDOW_S:
                    calls.popleft()
                if len(calls) < self.max_per_minute:
                    calls.append(now)
                    return
                wait = calls[0] + self.WINDOW_S - now
            self._sleep(wait)
