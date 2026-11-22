from datetime import date, datetime
from decimal import Decimal
import logging
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.config import get_settings
from app.db import get_db_connection
from app.services.litellm_client import BudgetStatus, LiteLLMClient

logger = logging.getLogger(__name__)

router = APIRouter()


class ModelReleaseTelemetry(BaseModel):
    id: int
    model_name: str
    provider: str
    release_date: date
    audited: bool
    created_at: datetime


class EvalRunTelemetry(BaseModel):
    id: int
    model_id: int
    model_name: str
    provider: str
    dataset_id: int
    category: str
    source: str
    passed: bool
    score: float
    cost_usd: float
    created_at: datetime


class BudgetTelemetry(BaseModel):
    daily_limit_usd: float
    spend_24h_usd: float
    remaining_usd: float
    utilization_pct: float
    source: Literal["litellm", "db_fallback", "unavailable"]


@router.get("/api/models", response_model=list[ModelReleaseTelemetry])
def list_model_releases() -> list[ModelReleaseTelemetry]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, model_name, provider, release_date, audited, created_at
                FROM model_releases
                ORDER BY audited ASC, release_date DESC, id DESC
                """
            )
            rows = cur.fetchall()
    return [ModelReleaseTelemetry(**dict(row)) for row in rows]


@router.get("/api/runs", response_model=list[EvalRunTelemetry])
def list_eval_runs(
    limit: int = Query(default=25, ge=1, le=200),
) -> list[EvalRunTelemetry]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    er.id,
                    er.model_id,
                    mr.model_name,
                    mr.provider,
                    er.dataset_id,
                    ed.category,
                    ed.source,
                    er.passed,
                    er.score,
                    er.cost_usd,
                    er.created_at
                FROM eval_results er
                JOIN model_releases mr ON mr.id = er.model_id
                JOIN eval_datasets ed ON ed.id = er.dataset_id
                ORDER BY er.created_at DESC, er.id DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

    results: list[EvalRunTelemetry] = []
    for row in rows:
        item = dict(row)
        item["score"] = float(item["score"])
        item["cost_usd"] = float(item["cost_usd"])
        results.append(EvalRunTelemetry(**item))
    return results


@router.get("/api/budget", response_model=BudgetTelemetry)
def get_budget_telemetry() -> BudgetTelemetry:
    daily_limit_usd = float(get_settings().daily_budget_usd)
    spend_24h_usd: float | None = None
    source: Literal["litellm", "db_fallback", "unavailable"] = "unavailable"

    try:
        client = LiteLLMClient()
        status_result = client.fetch_spend_usd(fallback_spent_usd=0.0)
        if isinstance(status_result, BudgetStatus):
            if status_result.source == "litellm-proxy":
                spend_24h_usd = float(status_result.spent_usd)
                source = "litellm"
        elif isinstance(status_result, (int, float, Decimal)):
            spend_24h_usd = float(status_result)
            source = "litellm"
    except Exception as exc:
        logger.warning("LiteLLM spend query failed (%s); falling back to PostgreSQL.", exc)

    if spend_24h_usd is None:
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT COALESCE(SUM(cost_usd), 0) AS spend_24h
                        FROM eval_results
                        WHERE created_at >= NOW() - INTERVAL '24 hours'
                        """
                    )
                    row = cur.fetchone()
            spend_24h_usd = float(row["spend_24h"]) if row and row["spend_24h"] is not None else 0.0
            source = "db_fallback"
        except Exception as exc:
            logger.warning("PostgreSQL budget fallback failed (%s); returning unavailable.", exc)
            spend_24h_usd = 0.0
            source = "unavailable"

    remaining_usd = round(max(0.0, daily_limit_usd - spend_24h_usd), 6)
    utilization_pct = (
        round(min(100.0, (spend_24h_usd / daily_limit_usd) * 100.0), 2)
        if daily_limit_usd > 0
        else 100.0
    )

    return BudgetTelemetry(
        daily_limit_usd=daily_limit_usd,
        spend_24h_usd=round(spend_24h_usd, 6),
        remaining_usd=remaining_usd,
        utilization_pct=utilization_pct,
        source=source,
    )
