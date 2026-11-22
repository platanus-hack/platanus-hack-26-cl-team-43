from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.db import get_db_connection

router = APIRouter()


class CategoryMetrics(BaseModel):
    avg_score: float
    passed: int
    total: int


class LeaderboardEntry(BaseModel):
    model_id: int
    model_name: str
    provider: str
    release_date: date | None
    audited: bool
    total_evals: int
    passed_evals: int
    pass_rate: float
    avg_score: float
    total_cost_usd: float
    last_audited_at: datetime | None
    categories: dict[str, CategoryMetrics]


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@router.get("/api/leaderboard", response_model=list[LeaderboardEntry])
def get_leaderboard() -> list[LeaderboardEntry]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    mr.id AS model_id,
                    mr.model_name,
                    mr.provider,
                    mr.release_date,
                    mr.audited,
                    ed.category,
                    COUNT(er.id) AS cat_total,
                    COALESCE(SUM(CASE WHEN er.passed THEN 1 ELSE 0 END), 0) AS cat_passed,
                    COALESCE(SUM(er.score), 0) AS cat_score_sum,
                    COALESCE(SUM(er.cost_usd), 0) AS cat_cost_usd,
                    MAX(er.created_at) AS cat_last_audited_at
                FROM model_releases mr
                LEFT JOIN eval_results er ON er.model_id = mr.id
                LEFT JOIN eval_datasets ed ON ed.id = er.dataset_id
                GROUP BY
                    mr.id,
                    mr.model_name,
                    mr.provider,
                    mr.release_date,
                    mr.audited,
                    ed.category
                ORDER BY mr.id DESC, ed.category ASC NULLS LAST
                """
            )
            rows = cur.fetchall()

    models: dict[int, dict[str, Any]] = {}

    for row in rows:
        item = dict(row)
        model_id = int(item["model_id"])
        acc = models.get(model_id)
        if acc is None:
            acc = {
                "model_id": model_id,
                "model_name": item["model_name"],
                "provider": item["provider"],
                "release_date": item.get("release_date"),
                "audited": bool(item.get("audited", False)),
                "total_evals": 0,
                "passed_evals": 0,
                "score_sum": Decimal("0"),
                "cost_sum": Decimal("0"),
                "last_audited_at": None,
                "categories": {},
            }
            models[model_id] = acc

        cat_total = int(item.get("cat_total") or 0)
        cat_passed = int(item.get("cat_passed") or 0)
        cat_score_sum = _to_decimal(item.get("cat_score_sum"))
        cat_cost_usd = _to_decimal(item.get("cat_cost_usd"))
        cat_last_audited_at = item.get("cat_last_audited_at")
        category = item.get("category")

        acc["total_evals"] += cat_total
        acc["passed_evals"] += cat_passed
        acc["score_sum"] += cat_score_sum
        acc["cost_sum"] += cat_cost_usd

        if cat_last_audited_at is not None:
            if acc["last_audited_at"] is None or cat_last_audited_at > acc["last_audited_at"]:
                acc["last_audited_at"] = cat_last_audited_at

        if category is not None and cat_total > 0:
            cat_acc = acc["categories"].get(category)
            if cat_acc is None:
                cat_acc = {
                    "total": 0,
                    "passed": 0,
                    "score_sum": Decimal("0"),
                }
                acc["categories"][category] = cat_acc
            cat_acc["total"] += cat_total
            cat_acc["passed"] += cat_passed
            cat_acc["score_sum"] += cat_score_sum

    entries: list[LeaderboardEntry] = []
    for acc in models.values():
        total_evals: int = acc["total_evals"]
        passed_evals: int = acc["passed_evals"]

        if total_evals > 0:
            pass_rate = round(passed_evals / total_evals, 4)
            avg_score = round(float(acc["score_sum"] / total_evals), 4)
        else:
            pass_rate = 0.0
            avg_score = 0.0

        total_cost_usd = round(float(acc["cost_sum"]), 6)

        sorted_categories: dict[str, CategoryMetrics] = {}
        for cat_name in sorted(acc["categories"].keys()):
            cat_acc = acc["categories"][cat_name]
            cat_total = cat_acc["total"]
            cat_avg = round(float(cat_acc["score_sum"] / cat_total), 4) if cat_total > 0 else 0.0
            sorted_categories[cat_name] = CategoryMetrics(
                avg_score=cat_avg,
                passed=cat_acc["passed"],
                total=cat_total,
            )

        entries.append(
            LeaderboardEntry(
                model_id=acc["model_id"],
                model_name=acc["model_name"],
                provider=acc["provider"],
                release_date=acc["release_date"],
                audited=acc["audited"],
                total_evals=total_evals,
                passed_evals=passed_evals,
                pass_rate=pass_rate,
                avg_score=avg_score,
                total_cost_usd=total_cost_usd,
                last_audited_at=acc["last_audited_at"],
                categories=sorted_categories,
            )
        )

    entries.sort(
        key=lambda entry: (
            -entry.avg_score,
            -entry.pass_rate,
            -entry.total_evals,
            -entry.model_id,
        )
    )
    return entries
