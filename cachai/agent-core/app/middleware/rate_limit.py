"""In-memory sliding-window rate limiting middleware for public API routes."""

from __future__ import annotations

from collections import defaultdict, deque
import math
import os
import threading
import time
from typing import Callable
from weakref import WeakSet

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

DEFAULT_TRIGGER_RPM = 10
DEFAULT_API_RPM = 120
DEFAULT_WINDOW_SECONDS = 60.0

_FALSY_VALUES = frozenset({"false", "0", "no", "off"})

RATE_LIMIT_DETAIL = "Rate limit exceeded. Please try again later."


def _parse_bool_env(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in _FALSY_VALUES


def _parse_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _extract_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        first_ip = forwarded_for.split(",", 1)[0].strip()
        if first_ip:
            return first_ip
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _classify_route_group(path: str) -> str | None:
    """Return the rate-limit route group for a path, or None if exempt."""
    if path == "/health" or path == "/health/":
        return None
    if path == "/api/trigger-demo" or path.startswith("/api/trigger-demo/"):
        return "trigger"
    if path == "/api" or path.startswith("/api/"):
        return "api"
    return None


class SlidingWindowRateLimiter:
    """Thread-safe in-memory sliding-window rate limiter."""

    def __init__(
        self,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_seconds = float(window_seconds)
        self._clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def is_allowed(self, key: tuple[str, str], limit: int) -> tuple[bool, int]:
        """Record a request if under the limit; return (allowed, retry_after_seconds)."""
        now = self._clock()
        cutoff = now - self._window_seconds

        with self._lock:
            timestamps = self._hits[key]
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) >= limit:
                if timestamps:
                    remaining = self._window_seconds - (now - timestamps[0])
                    retry_after = max(1, math.ceil(remaining))
                else:
                    retry_after = max(1, math.ceil(self._window_seconds))
                return False, retry_after

            timestamps.append(now)
            return True, 0

    def reset(self) -> None:
        """Clear all recorded request timestamps."""
        with self._lock:
            self._hits.clear()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP, per-route-group sliding-window rate limiter for `/api/*` endpoints."""

    _instances: WeakSet[RateLimitMiddleware] = WeakSet()

    def __init__(
        self,
        app: ASGIApp,
        *,
        trigger_rpm: int | None = None,
        api_rpm: int | None = None,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        enabled: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
        limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        super().__init__(app)
        self._trigger_rpm = (
            trigger_rpm
            if trigger_rpm is not None
            else _parse_positive_int_env("RATE_LIMIT_TRIGGER_RPM", DEFAULT_TRIGGER_RPM)
        )
        self._api_rpm = (
            api_rpm
            if api_rpm is not None
            else _parse_positive_int_env("RATE_LIMIT_API_RPM", DEFAULT_API_RPM)
        )
        self._enabled_override = enabled
        self._limiter = (
            limiter
            if limiter is not None
            else SlidingWindowRateLimiter(window_seconds=window_seconds, clock=clock)
        )
        RateLimitMiddleware._instances.add(self)

    def _is_enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return _parse_bool_env("RATE_LIMIT_ENABLED", default=True)

    def _limit_for_group(self, route_group: str) -> int:
        if route_group == "trigger":
            return self._trigger_rpm
        return self._api_rpm

    def reset(self) -> None:
        """Reset tracked request timestamps for this middleware instance."""
        self._limiter.reset()

    @classmethod
    def reset_all(cls) -> None:
        """Reset tracked request timestamps for all live middleware instances."""
        for instance in list(cls._instances):
            instance.reset()

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if not self._is_enabled():
            return await call_next(request)

        route_group = _classify_route_group(request.url.path)
        if route_group is None:
            return await call_next(request)

        client_ip = _extract_client_ip(request)
        limit = self._limit_for_group(route_group)
        allowed, retry_after = self._limiter.is_allowed((client_ip, route_group), limit)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": RATE_LIMIT_DETAIL},
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)
