"""Unit tests for BudgetCircuitBreaker, AgentBudgetLedger, and accountant integration."""

from collections.abc import Iterator
from typing import Any

import pytest

from app.agents import accountant as accountant_module
from app.services.circuit_breaker import (
    DEFAULT_AGENT_CAPS,
    AgentBudgetLedger,
    BudgetCircuitBreaker,
    CircuitState,
    get_circuit_breaker,
    reset_circuit_breaker,
)


class FakeClock:
    """Deterministic monotonic clock double for circuit breaker tests."""

    def __init__(self, initial: float = 1000.0) -> None:
        self.now: float = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def reset_global_circuit_breaker() -> Iterator[None]:
    """Ensure the process-wide circuit breaker never leaks state across tests."""
    reset_circuit_breaker()
    try:
        yield
    finally:
        reset_circuit_breaker()


def test_circuit_breaker_closed_open_half_open_closed_lifecycle() -> None:
    clock = FakeClock(100.0)
    breaker = BudgetCircuitBreaker(
        failure_threshold=3,
        recovery_timeout_seconds=60.0,
        clock=clock,
    )

    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow_request() is True
    assert breaker.status() == {
        "state": CircuitState.CLOSED.value,
        "failure_count": 0,
        "reason": None,
    }

    assert breaker.record_failure(status_code=500) is CircuitState.CLOSED
    assert breaker.record_failure(status_code=503) is CircuitState.CLOSED
    assert breaker.allow_request() is True
    assert breaker.status()["failure_count"] == 2

    # Third consecutive failure trips the circuit to OPEN.
    assert breaker.record_failure(status_code=429, reason="Rate limited") is CircuitState.OPEN
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow_request() is False
    open_status = breaker.status()
    assert open_status["state"] == CircuitState.OPEN.value
    assert open_status["failure_count"] == 3
    assert open_status["reason"] == "Rate limited"

    # Still OPEN before recovery_timeout_seconds elapses.
    clock.advance(59.9)
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow_request() is False

    # Transitions to HALF_OPEN once recovery timeout elapses.
    clock.advance(0.1)
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.allow_request() is True
    half_open_status = breaker.status()
    assert half_open_status["state"] == CircuitState.HALF_OPEN.value
    assert half_open_status["failure_count"] == 3

    # A successful probe closes the circuit and resets counters.
    assert breaker.record_success() is CircuitState.CLOSED
    assert breaker.allow_request() is True
    assert breaker.status() == {
        "state": CircuitState.CLOSED.value,
        "failure_count": 0,
        "reason": None,
    }


def test_circuit_breaker_half_open_retrips_on_probe_failure() -> None:
    clock = FakeClock(500.0)
    breaker = BudgetCircuitBreaker(
        failure_threshold=2,
        recovery_timeout_seconds=30.0,
        clock=clock,
    )

    breaker.record_failure(status_code=502)
    breaker.record_failure(status_code=503)
    assert breaker.state is CircuitState.OPEN

    clock.advance(30.0)
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.allow_request() is True

    # Single probe failure in HALF_OPEN immediately re-trips to OPEN.
    assert breaker.record_failure(status_code=500, reason="Probe failed") is CircuitState.OPEN
    assert breaker.allow_request() is False
    status = breaker.status()
    assert status["state"] == CircuitState.OPEN.value
    assert status["failure_count"] == 3
    assert status["reason"] == "Probe failed"

    # Recovery timer restarted from the re-trip timestamp.
    clock.advance(29.0)
    assert breaker.state is CircuitState.OPEN
    clock.advance(1.0)
    assert breaker.state is CircuitState.HALF_OPEN


def test_circuit_breaker_filters_upstream_status_codes() -> None:
    breaker = BudgetCircuitBreaker(failure_threshold=3)

    # Non-429 4xx client errors should be ignored.
    for code in (400, 401, 403, 404, 422):
        assert breaker.record_failure(status_code=code) is CircuitState.CLOSED

    assert breaker.status()["failure_count"] == 0
    assert breaker.allow_request() is True

    # 429, 5xx, and None (e.g. network/timeout error) count toward the threshold.
    assert breaker.record_failure(status_code=429) is CircuitState.CLOSED
    assert breaker.record_failure(status_code=500) is CircuitState.CLOSED
    assert breaker.status()["failure_count"] == 2

    # Another 404 must still not trip the breaker.
    assert breaker.record_failure(status_code=404) is CircuitState.CLOSED
    assert breaker.status()["failure_count"] == 2

    # 503 trips it.
    assert breaker.record_failure(status_code=503) is CircuitState.OPEN
    assert breaker.status()["failure_count"] == 3

    # Verify status_code=None also counts.
    breaker_none = BudgetCircuitBreaker(failure_threshold=1)
    assert breaker_none.record_failure(status_code=None) is CircuitState.OPEN
    assert breaker_none.status()["failure_count"] == 1


def test_circuit_breaker_spend_spike_trip() -> None:
    breaker = BudgetCircuitBreaker(spend_spike_threshold_usd=2.0)

    # Below and exactly at threshold remain CLOSED.
    assert breaker.record_spend_spike(1.50) is CircuitState.CLOSED
    assert breaker.record_spend_spike(2.00) is CircuitState.CLOSED
    assert breaker.allow_request() is True
    assert breaker.status()["reason"] is None

    # Above threshold trips to OPEN with an informative default reason.
    assert breaker.record_spend_spike(2.01) is CircuitState.OPEN
    assert breaker.allow_request() is False
    status = breaker.status()
    assert status["state"] == CircuitState.OPEN.value
    assert status["reason"] is not None
    assert "2.0100" in status["reason"]
    assert "2.0000" in status["reason"]

    # Custom reason is preserved when provided.
    breaker_custom = BudgetCircuitBreaker(spend_spike_threshold_usd=1.0)
    assert breaker_custom.record_spend_spike(1.5, reason="Runaway auditor loop") is CircuitState.OPEN
    assert breaker_custom.status()["reason"] == "Runaway auditor loop"

    # Negative spend is rejected.
    with pytest.raises(ValueError):
        breaker.record_spend_spike(-0.01)


def test_agent_budget_ledger_default_caps_spend_and_reset() -> None:
    ledger = AgentBudgetLedger()

    assert DEFAULT_AGENT_CAPS == {"scraper": 0.50, "auditor": 3.00, "judge": 1.50}
    assert ledger.spent("scraper") == 0.0
    assert ledger.remaining("scraper") == 0.50
    assert ledger.remaining("auditor") == 3.00
    assert ledger.remaining("judge") == 1.50

    assert ledger.can_spend("scraper", 0.50) is True
    assert ledger.can_spend("scraper", 0.51) is False

    assert ledger.record_spend("scraper", 0.20) == pytest.approx(0.20)
    assert ledger.record_spend("auditor", 1.25) == pytest.approx(1.25)
    assert ledger.record_spend("judge", 1.60) == pytest.approx(1.60)

    assert ledger.spent("scraper") == pytest.approx(0.20)
    assert ledger.remaining("scraper") == pytest.approx(0.30)
    assert ledger.can_spend("scraper", 0.30) is True
    assert ledger.can_spend("scraper", 0.31) is False

    # Overspend clamps remaining at 0.0.
    assert ledger.remaining("judge") == 0.0
    assert ledger.can_spend("judge", 0.01) is False

    summary = ledger.summary()
    assert set(summary.keys()) == {"scraper", "auditor", "judge"}
    assert summary["scraper"] == {"cap_usd": 0.50, "spent_usd": pytest.approx(0.20), "remaining_usd": pytest.approx(0.30)}
    assert summary["auditor"] == {"cap_usd": 3.00, "spent_usd": pytest.approx(1.25), "remaining_usd": pytest.approx(1.75)}
    assert summary["judge"] == {"cap_usd": 1.50, "spent_usd": pytest.approx(1.60), "remaining_usd": 0.0}

    ledger.reset()
    assert ledger.spent("scraper") == 0.0
    assert ledger.spent("auditor") == 0.0
    assert ledger.spent("judge") == 0.0
    assert ledger.remaining("judge") == 1.50


def test_agent_budget_ledger_custom_caps_and_validation() -> None:
    ledger = AgentBudgetLedger(caps={"custom_agent": 0.75})
    assert ledger.remaining("custom_agent") == 0.75
    assert ledger.can_spend("custom_agent", 0.75) is True

    with pytest.raises(ValueError, match="Unknown agent"):
        ledger.record_spend("scraper", 0.10)

    with pytest.raises(ValueError, match="Unknown agent"):
        ledger.spent("nonexistent")

    with pytest.raises(ValueError, match="Unknown agent"):
        ledger.remaining("nonexistent")

    with pytest.raises(ValueError, match="Unknown agent"):
        ledger.can_spend("nonexistent", 0.10)

    with pytest.raises(ValueError):
        ledger.record_spend("custom_agent", -0.05)

    with pytest.raises(ValueError):
        ledger.can_spend("custom_agent", -0.05)

    with pytest.raises(ValueError):
        AgentBudgetLedger(caps={"bad_agent": -1.0})


def test_accountant_node_short_circuits_when_circuit_breaker_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    breaker = reset_circuit_breaker(BudgetCircuitBreaker(failure_threshold=1))
    breaker.record_failure(status_code=429, reason="Upstream 429 burst")

    def fail_if_db_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("get_db_connection must not be called when circuit breaker is OPEN")

    monkeypatch.setattr(accountant_module, "get_db_connection", fail_if_db_called)

    result = accountant_module.accountant_node({})

    assert result["budget_approved"] is False
    assert result["budget_remaining_usd"] == 0.0
    assert "circuit breaker" in result["budget_reason"].lower()
    assert "Upstream 429 burst" in result["budget_reason"]


def test_circuit_breaker_singleton_get_and_reset() -> None:
    first = get_circuit_breaker()
    second = get_circuit_breaker()
    assert first is second

    replaced = reset_circuit_breaker()
    assert replaced is not first
    assert get_circuit_breaker() is replaced

    custom = BudgetCircuitBreaker(failure_threshold=5, recovery_timeout_seconds=120.0)
    installed = reset_circuit_breaker(custom)
    assert installed is custom
    assert get_circuit_breaker() is custom
