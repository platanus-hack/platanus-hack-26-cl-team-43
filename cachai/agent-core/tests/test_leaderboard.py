from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.leaderboard import router as leaderboard_router
from app.main import app as main_app


def _create_test_client() -> TestClient:
    app = FastAPI()
    app.include_router(leaderboard_router)
    return TestClient(app)


def _mock_db_connection(rows=None):
    cursor = MagicMock()
    cursor.fetchall.return_value = rows if rows is not None else []

    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False

    @contextmanager
    def _fake_get_db_connection():
        yield conn

    return _fake_get_db_connection, cursor


def test_leaderboard_aggregation_ordering_and_unaudited_model():
    t1 = datetime(2025, 2, 10, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2025, 2, 12, 15, 30, 0, tzinfo=timezone.utc)
    t3 = datetime(2025, 2, 14, 8, 45, 0, tzinfo=timezone.utc)

    rows = [
        # Model 1: avg_score = (0.9000 + 0.7000 + 0.8000) / 3 = 0.8000, pass_rate = 2/3 = 0.6667
        {
            "model_id": 1,
            "model_name": "gpt-4o-mini",
            "provider": "openai",
            "release_date": date(2025, 1, 15),
            "audited": True,
            "category": "pii_handling",
            "cat_total": 2,
            "cat_passed": 1,
            "cat_score_sum": Decimal("1.6000"),
            "cat_cost_usd": Decimal("0.002100"),
            "cat_last_audited_at": t1,
        },
        {
            "model_id": 1,
            "model_name": "gpt-4o-mini",
            "provider": "openai",
            "release_date": date(2025, 1, 15),
            "audited": True,
            "category": "hallucination_check",
            "cat_total": 1,
            "cat_passed": 1,
            "cat_score_sum": Decimal("0.8000"),
            "cat_cost_usd": Decimal("0.001234"),
            "cat_last_audited_at": t2,
        },
        # Model 2: avg_score = 0.9500, pass_rate = 1.0000 (should rank #1)
        {
            "model_id": 2,
            "model_name": "claude-3-5-haiku",
            "provider": "anthropic",
            "release_date": date(2025, 2, 1),
            "audited": True,
            "category": "instruction_hierarchy",
            "cat_total": 2,
            "cat_passed": 2,
            "cat_score_sum": Decimal("1.9000"),
            "cat_cost_usd": Decimal("0.004500"),
            "cat_last_audited_at": t3,
        },
        # Model 3: same avg_score as Model 1 (0.8000), higher pass_rate (1.0 vs 0.6667 -> ranks #2)
        {
            "model_id": 3,
            "model_name": "gemini-2.0-flash",
            "provider": "google",
            "release_date": date(2025, 2, 5),
            "audited": True,
            "category": "refusal_calibration",
            "cat_total": 2,
            "cat_passed": 2,
            "cat_score_sum": Decimal("1.6000"),
            "cat_cost_usd": Decimal("0.000900"),
            "cat_last_audited_at": t1,
        },
        # Model 4: unaudited model with 0 evals (LEFT JOIN produces NULLs/zeros -> ranks last)
        {
            "model_id": 4,
            "model_name": "llama-3.1-8b-instruct",
            "provider": "meta",
            "release_date": date(2025, 2, 10),
            "audited": False,
            "category": None,
            "cat_total": 0,
            "cat_passed": 0,
            "cat_score_sum": None,
            "cat_cost_usd": None,
            "cat_last_audited_at": None,
        },
    ]

    fake_db, cursor = _mock_db_connection(rows=rows)
    client = _create_test_client()

    with patch("app.api.leaderboard.get_db_connection", fake_db):
        response = client.get("/api/leaderboard")

    assert response.status_code == 200
    data = response.json()
    assert [entry["model_id"] for entry in data] == [2, 3, 1, 4]

    # Rank #1: claude-3-5-haiku
    assert data[0]["model_name"] == "claude-3-5-haiku"
    assert data[0]["total_evals"] == 2
    assert data[0]["passed_evals"] == 2
    assert data[0]["pass_rate"] == 1.0
    assert data[0]["avg_score"] == 0.95
    assert data[0]["total_cost_usd"] == 0.0045
    assert data[0]["last_audited_at"] == "2025-02-14T08:45:00Z"

    # Rank #2: gemini-2.0-flash (wins tie-break on pass_rate over gpt-4o-mini)
    assert data[1]["model_name"] == "gemini-2.0-flash"
    assert data[1]["avg_score"] == 0.8
    assert data[1]["pass_rate"] == 1.0

    # Rank #3: gpt-4o-mini (aggregated across 2 categories, latest timestamp picked)
    assert data[2]["model_name"] == "gpt-4o-mini"
    assert data[2]["total_evals"] == 3
    assert data[2]["passed_evals"] == 2
    assert data[2]["pass_rate"] == 0.6667
    assert data[2]["avg_score"] == 0.8
    assert data[2]["total_cost_usd"] == 0.003334
    assert data[2]["last_audited_at"] == "2025-02-12T15:30:00Z"

    # Rank #4: llama-3.1-8b-instruct (0 evals)
    assert data[3] == {
        "model_id": 4,
        "model_name": "llama-3.1-8b-instruct",
        "provider": "meta",
        "release_date": "2025-02-10",
        "audited": False,
        "total_evals": 0,
        "passed_evals": 0,
        "pass_rate": 0.0,
        "avg_score": 0.0,
        "total_cost_usd": 0.0,
        "last_audited_at": None,
        "categories": {},
    }
    cursor.execute.assert_called_once()


def test_leaderboard_empty_state():
    fake_db, cursor = _mock_db_connection(rows=[])
    client = _create_test_client()

    with patch("app.api.leaderboard.get_db_connection", fake_db):
        response = client.get("/api/leaderboard")

    assert response.status_code == 200
    assert response.json() == []
    cursor.execute.assert_called_once()


def test_leaderboard_category_breakdown_and_deterministic_sorting():
    now = datetime(2025, 2, 15, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        {
            "model_id": 10,
            "model_name": "gpt-4o-mini",
            "provider": "openai",
            "release_date": date(2025, 1, 15),
            "audited": True,
            "category": "refusal_calibration",
            "cat_total": 3,
            "cat_passed": 2,
            "cat_score_sum": Decimal("2.5000"),
            "cat_cost_usd": Decimal("0.003000"),
            "cat_last_audited_at": now,
        },
        {
            "model_id": 10,
            "model_name": "gpt-4o-mini",
            "provider": "openai",
            "release_date": date(2025, 1, 15),
            "audited": True,
            "category": "hallucination_check",
            "cat_total": 2,
            "cat_passed": 2,
            "cat_score_sum": Decimal("1.9500"),
            "cat_cost_usd": Decimal("0.002000"),
            "cat_last_audited_at": now,
        },
        {
            "model_id": 10,
            "model_name": "gpt-4o-mini",
            "provider": "openai",
            "release_date": date(2025, 1, 15),
            "audited": True,
            "category": "pii_handling",
            "cat_total": 4,
            "cat_passed": 3,
            "cat_score_sum": Decimal("3.1000"),
            "cat_cost_usd": Decimal("0.004000"),
            "cat_last_audited_at": now,
        },
        {
            "model_id": 11,
            "model_name": "mistral-small",
            "provider": "mistral",
            "release_date": None,
            "audited": False,
            "category": None,
            "cat_total": 0,
            "cat_passed": 0,
            "cat_score_sum": Decimal("0"),
            "cat_cost_usd": Decimal("0"),
            "cat_last_audited_at": None,
        },
    ]

    fake_db, _ = _mock_db_connection(rows=rows)
    client = _create_test_client()

    with patch("app.api.leaderboard.get_db_connection", fake_db):
        response = client.get("/api/leaderboard")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2

    audited_entry = data[0]
    assert audited_entry["model_id"] == 10
    assert list(audited_entry["categories"].keys()) == [
        "hallucination_check",
        "pii_handling",
        "refusal_calibration",
    ]
    assert audited_entry["categories"] == {
        "hallucination_check": {"avg_score": 0.975, "passed": 2, "total": 2},
        "pii_handling": {"avg_score": 0.775, "passed": 3, "total": 4},
        "refusal_calibration": {"avg_score": 0.8333, "passed": 2, "total": 3},
    }
    # Weighted avg across all 9 evals: (2.5 + 1.95 + 3.1) / 9 = 7.55 / 9 = 0.8389
    assert audited_entry["total_evals"] == 9
    assert audited_entry["passed_evals"] == 7
    assert audited_entry["avg_score"] == 0.8389
    assert audited_entry["pass_rate"] == 0.7778
    assert audited_entry["total_cost_usd"] == 0.009

    unaudited_entry = data[1]
    assert unaudited_entry["model_id"] == 11
    assert unaudited_entry["release_date"] is None
    assert unaudited_entry["categories"] == {}


def test_leaderboard_route_registered_on_main_app():
    openapi_paths = main_app.openapi()["paths"]
    assert "/api/leaderboard" in openapi_paths
    assert "get" in openapi_paths["/api/leaderboard"]
