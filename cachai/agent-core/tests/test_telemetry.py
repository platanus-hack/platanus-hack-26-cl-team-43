from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.telemetry import router as telemetry_router
from app.services.litellm_client import BudgetStatus


def _create_test_client() -> TestClient:
    app = FastAPI()
    app.include_router(telemetry_router)
    return TestClient(app)


def _mock_db_connection(rows=None, single_row=None):
    cursor = MagicMock()
    cursor.fetchall.return_value = rows if rows is not None else []
    cursor.fetchone.return_value = single_row

    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False

    @contextmanager
    def _fake_get_db_connection():
        yield conn

    return _fake_get_db_connection, cursor


def test_list_models_returns_serialized_rows():
    now = datetime(2025, 2, 15, 12, 30, 0, tzinfo=timezone.utc)
    rows = [
        {
            "id": 3,
            "model_name": "llama-3.1-8b-instruct",
            "provider": "meta",
            "release_date": date(2025, 2, 10),
            "audited": False,
            "created_at": now,
        },
        {
            "id": 1,
            "model_name": "gpt-4o-mini",
            "provider": "openai",
            "release_date": date(2025, 1, 15),
            "audited": True,
            "created_at": now,
        },
    ]
    fake_db, cursor = _mock_db_connection(rows=rows)
    client = _create_test_client()

    with patch("app.api.telemetry.get_db_connection", fake_db):
        response = client.get("/api/models")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0] == {
        "id": 3,
        "model_name": "llama-3.1-8b-instruct",
        "provider": "meta",
        "release_date": "2025-02-10",
        "audited": False,
        "created_at": "2025-02-15T12:30:00Z",
    }
    assert data[1]["audited"] is True
    cursor.execute.assert_called_once()


def test_list_models_empty():
    fake_db, _ = _mock_db_connection(rows=[])
    client = _create_test_client()

    with patch("app.api.telemetry.get_db_connection", fake_db):
        response = client.get("/api/models")

    assert response.status_code == 200
    assert response.json() == []


def test_list_runs_casts_decimals_and_applies_default_limit():
    now = datetime(2025, 2, 15, 14, 0, 0, tzinfo=timezone.utc)
    rows = [
        {
            "id": 10,
            "model_id": 1,
            "model_name": "gpt-4o-mini",
            "provider": "openai",
            "dataset_id": 2,
            "category": "pii_handling",
            "source": "seed-baseline-v1",
            "passed": True,
            "score": Decimal("0.9250"),
            "cost_usd": Decimal("0.001500"),
            "created_at": now,
        }
    ]
    fake_db, cursor = _mock_db_connection(rows=rows)
    client = _create_test_client()

    with patch("app.api.telemetry.get_db_connection", fake_db):
        response = client.get("/api/runs")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["score"] == 0.925
    assert isinstance(data[0]["score"], float)
    assert data[0]["cost_usd"] == 0.0015
    assert isinstance(data[0]["cost_usd"], float)
    assert data[0]["category"] == "pii_handling"
    assert cursor.execute.call_args.args[1] == (25,)


def test_list_runs_custom_limit_and_validation():
    fake_db, cursor = _mock_db_connection(rows=[])
    client = _create_test_client()

    with patch("app.api.telemetry.get_db_connection", fake_db):
        ok_resp = client.get("/api/runs?limit=5")
        low_resp = client.get("/api/runs?limit=0")
        high_resp = client.get("/api/runs?limit=201")

    assert ok_resp.status_code == 200
    assert ok_resp.json() == []
    assert cursor.execute.call_args.args[1] == (5,)
    assert low_resp.status_code == 422
    assert high_resp.status_code == 422


def test_budget_litellm_primary_path():
    client = _create_test_client()
    mock_litellm = MagicMock()
    mock_litellm.fetch_spend_usd.return_value = BudgetStatus(
        spent_usd=1.25,
        daily_limit_usd=5.0,
        remaining_usd=3.75,
        source="litellm-proxy",
    )
    mock_db = MagicMock()

    with (
        patch("app.api.telemetry.get_settings", return_value=SimpleNamespace(daily_budget_usd=5.0)),
        patch("app.api.telemetry.LiteLLMClient", return_value=mock_litellm),
        patch("app.api.telemetry.get_db_connection", mock_db),
    ):
        response = client.get("/api/budget")

    assert response.status_code == 200
    assert response.json() == {
        "daily_limit_usd": 5.0,
        "spend_24h_usd": 1.25,
        "remaining_usd": 3.75,
        "utilization_pct": 25.0,
        "source": "litellm",
    }
    mock_db.assert_not_called()


def test_budget_db_fallback_when_litellm_uses_fallback_status():
    client = _create_test_client()
    mock_litellm = MagicMock()
    mock_litellm.fetch_spend_usd.return_value = BudgetStatus(
        spent_usd=0.0,
        daily_limit_usd=5.0,
        remaining_usd=5.0,
        source="local-db-fallback",
    )
    fake_db, _ = _mock_db_connection(single_row={"spend_24h": Decimal("2.500000")})

    with (
        patch("app.api.telemetry.get_settings", return_value=SimpleNamespace(daily_budget_usd=5.0)),
        patch("app.api.telemetry.LiteLLMClient", return_value=mock_litellm),
        patch("app.api.telemetry.get_db_connection", fake_db),
    ):
        response = client.get("/api/budget")

    assert response.status_code == 200
    assert response.json() == {
        "daily_limit_usd": 5.0,
        "spend_24h_usd": 2.5,
        "remaining_usd": 2.5,
        "utilization_pct": 50.0,
        "source": "db_fallback",
    }


def test_budget_db_fallback_when_litellm_returns_none_or_raises():
    client = _create_test_client()
    fake_db, _ = _mock_db_connection(single_row={"spend_24h": Decimal("0.500000")})

    for side_effect in (None, RuntimeError("proxy unreachable")):
        mock_litellm = MagicMock()
        if side_effect is None:
            mock_litellm.fetch_spend_usd.return_value = None
        else:
            mock_litellm.fetch_spend_usd.side_effect = side_effect

        with (
            patch("app.api.telemetry.get_settings", return_value=SimpleNamespace(daily_budget_usd=5.0)),
            patch("app.api.telemetry.LiteLLMClient", return_value=mock_litellm),
            patch("app.api.telemetry.get_db_connection", fake_db),
        ):
            response = client.get("/api/budget")

        assert response.status_code == 200
        assert response.json() == {
            "daily_limit_usd": 5.0,
            "spend_24h_usd": 0.5,
            "remaining_usd": 4.5,
            "utilization_pct": 10.0,
            "source": "db_fallback",
        }


def test_budget_unavailable_when_litellm_and_db_both_fail():
    client = _create_test_client()
    mock_litellm = MagicMock()
    mock_litellm.fetch_spend_usd.side_effect = RuntimeError("proxy down")

    @contextmanager
    def _failing_db():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    with (
        patch("app.api.telemetry.get_settings", return_value=SimpleNamespace(daily_budget_usd=5.0)),
        patch("app.api.telemetry.LiteLLMClient", return_value=mock_litellm),
        patch("app.api.telemetry.get_db_connection", _failing_db),
    ):
        response = client.get("/api/budget")

    assert response.status_code == 200
    assert response.json() == {
        "daily_limit_usd": 5.0,
        "spend_24h_usd": 0.0,
        "remaining_usd": 5.0,
        "utilization_pct": 0.0,
        "source": "unavailable",
    }


def test_budget_clamps_overspend():
    client = _create_test_client()
    mock_litellm = MagicMock()
    mock_litellm.fetch_spend_usd.return_value = BudgetStatus(
        spent_usd=7.5,
        daily_limit_usd=5.0,
        remaining_usd=0.0,
        source="litellm-proxy",
    )

    with (
        patch("app.api.telemetry.get_settings", return_value=SimpleNamespace(daily_budget_usd=5.0)),
        patch("app.api.telemetry.LiteLLMClient", return_value=mock_litellm),
    ):
        response = client.get("/api/budget")

    assert response.status_code == 200
    assert response.json() == {
        "daily_limit_usd": 5.0,
        "spend_24h_usd": 7.5,
        "remaining_usd": 0.0,
        "utilization_pct": 100.0,
        "source": "litellm",
    }
