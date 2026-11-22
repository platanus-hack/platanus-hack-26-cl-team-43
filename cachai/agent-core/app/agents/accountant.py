import logging
from typing import Any

from app.agents.state import AuditGraphState
from app.config import get_settings
from app.db import get_db_connection
from app.services.circuit_breaker import get_circuit_breaker
from app.services.litellm_client import LiteLLMClient

logger = logging.getLogger(__name__)


def accountant_node(state: AuditGraphState) -> dict[str, Any]:
    """Check circuit breaker and remaining budget via LiteLLM Proxy (or local DB ledger) before auditing."""
    breaker = get_circuit_breaker()
    if not breaker.allow_request():
        status = breaker.status()
        reason = f"Rejected (circuit breaker {status['state']}): {status['reason'] or 'circuit open'}"
        logger.warning("Accountant decision: %s", reason)
        return {
            "budget_approved": False,
            "budget_remaining_usd": 0.0,
            "budget_reason": reason,
        }

    settings = get_settings()
    client = LiteLLMClient()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0)::float AS spent_today
                FROM eval_results
                WHERE created_at >= NOW() - INTERVAL '24 hours'
                """
            )
            row = cur.fetchone()
            local_spent = float(row["spent_today"]) if row else 0.0

    budget = client.fetch_spend_usd(fallback_spent_usd=local_spent)
    required = settings.estimated_audit_cost_usd
    approved = budget.remaining_usd >= required

    reason = (
        f"Approved ({budget.source}): remaining ${budget.remaining_usd:.4f} >= estimated ${required:.4f}"
        if approved
        else f"Rejected ({budget.source}): remaining ${budget.remaining_usd:.4f} < estimated ${required:.4f}"
    )
    logger.info("Accountant decision: %s", reason)

    return {
        "budget_approved": approved,
        "budget_remaining_usd": budget.remaining_usd,
        "budget_reason": reason,
    }
