import logging
from typing import Any

from app.agents.state import AuditGraphState
from app.db import get_db_connection

logger = logging.getLogger(__name__)


def orchestrator_node(state: AuditGraphState) -> dict[str, Any]:
    """Inspect model_releases and select the next unaudited model for evaluation."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, model_name, provider, release_date::text AS release_date, audited
                FROM model_releases
                WHERE audited = FALSE
                ORDER BY release_date DESC, id ASC
                LIMIT 1
                """
            )
            row = cur.fetchone()

    if not row:
        logger.info("Orchestrator found no unaudited model releases.")
        return {"target_model": None}

    logger.info(
        "Orchestrator selected model %s (%s) [id=%s] for audit.",
        row["model_name"],
        row["provider"],
        row["id"],
    )
    return {"target_model": dict(row)}
