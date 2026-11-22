"""Per-agent budget ledger and LLM spend/failure circuit breaker."""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum
from typing import Any


class CircuitState(str, Enum):
    """Lifecycle states for the budget/upstream circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


DEFAULT_AGENT_CAPS: dict[str, float] = {
    "scraper": 0.50,
    "auditor": 3.00,
    "judge": 1.50,
}


class AgentBudgetLedger:
    """Tracks per-agent USD spend against configured budget caps."""

    def __init__(self, caps: dict[str, float] | None = None) -> None:
        raw_caps = DEFAULT_AGENT_CAPS if caps is None else caps
        validated_caps: dict[str, float] = {}
        for agent, cap in raw_caps.items():
            if cap < 0.0:
                raise ValueError(f"Budget cap for agent '{agent}' must be >= 0.0, got {cap}")
            validated_caps[agent] = float(cap)

        self._caps: dict[str, float] = validated_caps
        self._spent: dict[str, float] = {agent: 0.0 for agent in self._caps}

    def _validate_agent(self, agent: str) -> None:
        if agent not in self._caps:
            raise ValueError(f"Unknown agent '{agent}'. Configured agents: {sorted(self._caps)}")

    def record_spend(self, agent: str, usd: float) -> float:
        """Record USD spend for an agent and return its updated cumulative spend."""
        self._validate_agent(agent)
        if usd < 0.0:
            raise ValueError(f"Spend amount must be >= 0.0, got {usd}")
        self._spent[agent] += float(usd)
        return self._spent[agent]

    def spent(self, agent: str) -> float:
        """Return cumulative USD spent by an agent."""
        self._validate_agent(agent)
        return self._spent[agent]

    def remaining(self, agent: str) -> float:
        """Return remaining USD budget for an agent, clamped at 0.0."""
        self._validate_agent(agent)
        return max(0.0, round(self._caps[agent] - self._spent[agent], 6))

    def can_spend(self, agent: str, estimated_usd: float) -> bool:
        """Return True if the agent has enough remaining budget for estimated_usd."""
        self._validate_agent(agent)
        if estimated_usd < 0.0:
            raise ValueError(f"Estimated spend must be >= 0.0, got {estimated_usd}")
        return self.remaining(agent) >= estimated_usd

    def reset(self) -> None:
        """Reset all per-agent spend counters to 0.0."""
        for agent in self._spent:
            self._spent[agent] = 0.0

    def summary(self) -> dict[str, dict[str, float]]:
        """Return a per-agent snapshot of cap, spent, and remaining USD."""
        return {
            agent: {
                "cap_usd": cap,
                "spent_usd": self._spent[agent],
                "remaining_usd": self.remaining(agent),
            }
            for agent, cap in self._caps.items()
        }


class BudgetCircuitBreaker:
    """Circuit breaker that trips on consecutive transient failures or spend spikes."""

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 60.0,
        spend_spike_threshold_usd: float = 2.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >= 1, got {failure_threshold}")
        if recovery_timeout_seconds < 0.0:
            raise ValueError(
                f"recovery_timeout_seconds must be >= 0.0, got {recovery_timeout_seconds}"
            )
        if spend_spike_threshold_usd < 0.0:
            raise ValueError(
                f"spend_spike_threshold_usd must be >= 0.0, got {spend_spike_threshold_usd}"
            )

        self.failure_threshold: int = failure_threshold
        self.recovery_timeout_seconds: float = float(recovery_timeout_seconds)
        self.spend_spike_threshold_usd: float = float(spend_spike_threshold_usd)
        self._clock: Callable[[], float] = clock or time.monotonic

        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._reason: str | None = None
        self._opened_at: float | None = None

    def _maybe_transition_to_half_open(self) -> None:
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and (self._clock() - self._opened_at) >= self.recovery_timeout_seconds
        ):
            self._state = CircuitState.HALF_OPEN

    @property
    def state(self) -> CircuitState:
        """Return the current circuit state, transitioning OPEN -> HALF_OPEN if elapsed."""
        self._maybe_transition_to_half_open()
        return self._state

    def allow_request(self) -> bool:
        """Return True if requests are currently permitted through the circuit."""
        return self.state is not CircuitState.OPEN

    def record_failure(
        self,
        status_code: int | None = None,
        reason: str | None = None,
    ) -> CircuitState:
        """Record an upstream failure and open the circuit if threshold is reached."""
        self._maybe_transition_to_half_open()
        if status_code is not None and status_code != 429 and not (500 <= status_code < 600):
            return self._state

        self._failure_count += 1
        if self._state is CircuitState.HALF_OPEN or self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()
            self._reason = (
                reason
                or f"Consecutive upstream failures reached threshold ({self._failure_count}/{self.failure_threshold})"
            )
        return self._state

    def record_spend_spike(
        self,
        spend_usd: float,
        reason: str | None = None,
    ) -> CircuitState:
        """Record a spend amount and open the circuit if it exceeds the spike threshold."""
        if spend_usd < 0.0:
            raise ValueError(f"spend_usd must be >= 0.0, got {spend_usd}")

        self._maybe_transition_to_half_open()
        if spend_usd > self.spend_spike_threshold_usd:
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()
            self._reason = (
                reason
                or f"Spend spike ${spend_usd:.4f} exceeded threshold ${self.spend_spike_threshold_usd:.4f}"
            )
        return self._state

    def record_success(self) -> CircuitState:
        """Reset the circuit breaker to CLOSED after a successful probe or call."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._reason = None
        self._opened_at = None
        return self._state

    def status(self) -> dict[str, Any]:
        """Return a serializable snapshot of the circuit breaker status."""
        self._maybe_transition_to_half_open()
        return {
            "state": self._state.value,
            "failure_count": self._failure_count,
            "reason": self._reason,
        }


_circuit_breaker: BudgetCircuitBreaker | None = None


def get_circuit_breaker() -> BudgetCircuitBreaker:
    """Return the process-wide BudgetCircuitBreaker singleton, creating it lazily."""
    global _circuit_breaker
    if _circuit_breaker is None:
        _circuit_breaker = BudgetCircuitBreaker()
    return _circuit_breaker


def reset_circuit_breaker(
    breaker: BudgetCircuitBreaker | None = None,
) -> BudgetCircuitBreaker:
    """Replace the singleton BudgetCircuitBreaker and return the active instance."""
    global _circuit_breaker
    _circuit_breaker = breaker if breaker is not None else BudgetCircuitBreaker()
    return _circuit_breaker
