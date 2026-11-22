"""Unit tests for the FastAPI sliding-window rate limiting middleware."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
import pytest

from app import main
from app.middleware import RateLimitMiddleware, SlidingWindowRateLimiter


class FakeClock:
    """Deterministic monotonic clock double for sliding-window tests."""

    def __init__(self, initial: float = 1000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _build_test_app(
    *,
    trigger_rpm: int | None = 3,
    api_rpm: int | None = 5,
    window_seconds: float = 60.0,
    enabled: bool | None = None,
    clock: FakeClock | None = None,
    limiter: SlidingWindowRateLimiter | None = None,
) -> FastAPI:
    """Create a minimal FastAPI app wired with RateLimitMiddleware and CORS."""
    test_app = FastAPI()

    middleware_kwargs: dict = {
        "trigger_rpm": trigger_rpm,
        "api_rpm": api_rpm,
        "window_seconds": window_seconds,
        "enabled": enabled,
    }
    if clock is not None:
        middleware_kwargs["clock"] = clock
    if limiter is not None:
        middleware_kwargs["limiter"] = limiter

    test_app.add_middleware(RateLimitMiddleware, **middleware_kwargs)
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @test_app.get("/")
    async def root() -> dict[str, str]:
        return {"status": "ok"}

    @test_app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @test_app.post("/api/trigger-demo")
    async def trigger_demo() -> dict[str, str]:
        return {"status": "triggered"}

    @test_app.get("/api/posts")
    async def list_posts() -> list[dict[str, str]]:
        return []

    @test_app.get("/api/leaderboard")
    async def leaderboard() -> dict[str, list]:
        return {"entries": []}

    return test_app


def _find_rate_limit_middleware(app: FastAPI) -> RateLimitMiddleware:
    """Traverse Starlette's built middleware stack to locate RateLimitMiddleware."""
    current = app.middleware_stack
    while current is not None:
        if isinstance(current, RateLimitMiddleware):
            return current
        current = getattr(current, "app", None)
    raise AssertionError("RateLimitMiddleware not found in middleware stack")


def test_requests_within_limit_pass() -> None:
    app = _build_test_app(trigger_rpm=3, api_rpm=5)
    client = TestClient(app)

    for _ in range(3):
        res = client.post("/api/trigger-demo")
        assert res.status_code == 200

    for _ in range(5):
        res = client.get("/api/posts")
        assert res.status_code == 200


def test_trigger_demo_returns_429_with_retry_after_and_detail() -> None:
    clock = FakeClock(initial=100.0)
    app = _build_test_app(trigger_rpm=2, api_rpm=10, window_seconds=60.0, clock=clock)
    client = TestClient(app)

    assert client.post("/api/trigger-demo").status_code == 200
    clock.advance(10.0)
    assert client.post("/api/trigger-demo").status_code == 200

    clock.advance(5.0)  # now=115.0, oldest hit at 100.0 -> 45s remaining
    response = client.post(
        "/api/trigger-demo",
        headers={"Origin": "http://localhost:3000"},
    )

    assert response.status_code == 429
    assert response.json() == {"detail": "Rate limit exceeded. Please try again later."}
    retry_after = response.headers.get("Retry-After")
    assert retry_after is not None
    assert retry_after.isdigit()
    assert int(retry_after) == 45
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_api_routes_share_api_group_and_return_429() -> None:
    clock = FakeClock(initial=500.0)
    app = _build_test_app(trigger_rpm=10, api_rpm=3, window_seconds=60.0, clock=clock)
    client = TestClient(app)

    assert client.get("/api/posts").status_code == 200
    assert client.get("/api/leaderboard").status_code == 200
    assert client.get("/api/posts").status_code == 200

    blocked = client.get("/api/leaderboard")
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Rate limit exceeded. Please try again later."}
    retry_after = blocked.headers.get("Retry-After")
    assert retry_after is not None and int(retry_after) >= 1


def test_health_and_non_api_routes_are_exempt() -> None:
    app = _build_test_app(trigger_rpm=1, api_rpm=1)
    client = TestClient(app)

    for _ in range(25):
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200


def test_reset_clears_state_and_allows_requests_again() -> None:
    clock = FakeClock(initial=200.0)
    app = _build_test_app(trigger_rpm=2, api_rpm=2, clock=clock)
    client = TestClient(app)

    assert client.post("/api/trigger-demo").status_code == 200
    assert client.post("/api/trigger-demo").status_code == 200
    assert client.post("/api/trigger-demo").status_code == 429

    middleware = _find_rate_limit_middleware(app)
    middleware.reset()

    assert client.post("/api/trigger-demo").status_code == 200
    assert client.post("/api/trigger-demo").status_code == 200


def test_sliding_window_expiry_allows_requests_after_window_passes() -> None:
    clock = FakeClock(initial=1000.0)
    app = _build_test_app(trigger_rpm=2, api_rpm=5, window_seconds=60.0, clock=clock)
    client = TestClient(app)

    assert client.post("/api/trigger-demo").status_code == 200  # t=1000
    clock.advance(20.0)
    assert client.post("/api/trigger-demo").status_code == 200  # t=1020
    assert client.post("/api/trigger-demo").status_code == 429  # t=1020

    # Advance 40s -> t=1060; the t=1000 hit expires, opening 1 slot
    clock.advance(40.0)
    assert client.post("/api/trigger-demo").status_code == 200  # t=1060
    assert client.post("/api/trigger-demo").status_code == 429  # t=1060

    # Advance 60s -> all prior hits expire
    clock.advance(60.0)
    assert client.post("/api/trigger-demo").status_code == 200
    assert client.post("/api/trigger-demo").status_code == 200


@pytest.mark.parametrize("env_value", ["false", "FALSE", "0", "no", "off"])
def test_rate_limit_enabled_env_false_disables_limiting(
    monkeypatch: pytest.MonkeyPatch, env_value: str
) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", env_value)
    app = _build_test_app(trigger_rpm=1, api_rpm=1, enabled=None)
    client = TestClient(app)

    for _ in range(5):
        assert client.post("/api/trigger-demo").status_code == 200
        assert client.get("/api/posts").status_code == 200


def test_env_vars_configure_default_rpm_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATE_LIMIT_TRIGGER_RPM", "2")
    monkeypatch.setenv("RATE_LIMIT_API_RPM", "3")
    app = _build_test_app(trigger_rpm=None, api_rpm=None)
    client = TestClient(app)

    assert client.post("/api/trigger-demo").status_code == 200
    assert client.post("/api/trigger-demo").status_code == 200
    assert client.post("/api/trigger-demo").status_code == 429

    assert client.get("/api/posts").status_code == 200
    assert client.get("/api/posts").status_code == 200
    assert client.get("/api/posts").status_code == 200
    assert client.get("/api/posts").status_code == 429


def test_different_client_ips_and_route_groups_are_isolated() -> None:
    app = _build_test_app(trigger_rpm=1, api_rpm=2)
    client = TestClient(app)

    ip_a = {"X-Forwarded-For": "203.0.113.10, 10.0.0.1"}
    ip_b = {"X-Forwarded-For": "198.51.100.20"}

    # Exhaust trigger group for IP A
    assert client.post("/api/trigger-demo", headers=ip_a).status_code == 200
    assert client.post("/api/trigger-demo", headers=ip_a).status_code == 429

    # IP A still has full quota on the api route group
    assert client.get("/api/posts", headers=ip_a).status_code == 200
    assert client.get("/api/posts", headers=ip_a).status_code == 200
    assert client.get("/api/posts", headers=ip_a).status_code == 429

    # IP B is completely unaffected by IP A
    assert client.post("/api/trigger-demo", headers=ip_b).status_code == 200
    assert client.get("/api/posts", headers=ip_b).status_code == 200


def test_main_app_registers_rate_limit_inside_cors() -> None:
    middleware_classes = [m.cls for m in main.app.user_middleware]
    assert RateLimitMiddleware in middleware_classes
    assert CORSMiddleware in middleware_classes
    # In FastAPI/Starlette, user_middleware is stored outermost-first (inserted at 0),
    # so CORSMiddleware must appear before RateLimitMiddleware.
    assert middleware_classes.index(CORSMiddleware) < middleware_classes.index(
        RateLimitMiddleware
    )
