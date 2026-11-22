from typing import Any

import httpx
import pytest

from app.agents import accountant as accountant_module
from app.agents import graph as graph_module
from app.config import get_settings


def _sample_target_model() -> dict[str, Any]:
    return {
        "id": 1,
        "model_name": "gpt-4o-mini",
        "provider": "openai",
        "release_date": "2025-01-15",
        "audited": False,
    }


def test_audit_graph_happy_path_runs_all_nodes_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    executed_nodes: list[str] = []

    def fake_scraper(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("scraper")
        return {"scraped_models": 2, "scraped_prompts": 1}

    def fake_orchestrator(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("orchestrator")
        return {"target_model": _sample_target_model()}

    def fake_accountant(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("accountant")
        return {
            "budget_approved": True,
            "budget_remaining_usd": 4.75,
            "budget_reason": "Approved (local-db-fallback): remaining $4.7500 >= estimated $0.2500",
        }

    def fake_auditor(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("auditor")
        return {
            "eval_results": [{"eval_id": 10, "category": "pii_handling", "passed": True, "score": 1.0}],
            "published_post_slug": "audit-openai-gpt-4o-mini",
            "errors": [],
        }

    monkeypatch.setattr(graph_module, "scraper_node", fake_scraper)
    monkeypatch.setattr(graph_module, "orchestrator_node", fake_orchestrator)
    monkeypatch.setattr(graph_module, "accountant_node", fake_accountant)
    monkeypatch.setattr(graph_module, "auditor_node", fake_auditor)
    monkeypatch.setattr(graph_module, "audit_graph", graph_module.build_audit_graph())

    result = graph_module.run_audit_cycle(trigger_source="test-happy")

    assert executed_nodes == ["scraper", "orchestrator", "accountant", "auditor"]
    assert result["trigger_source"] == "test-happy"
    assert result["scraped_models"] == 2
    assert result["scraped_prompts"] == 1
    assert result["target_model"]["model_name"] == "gpt-4o-mini"
    assert result["budget_approved"] is True
    assert result["budget_remaining_usd"] == 4.75
    assert result["published_post_slug"] == "audit-openai-gpt-4o-mini"
    assert len(result["eval_results"]) == 1
    assert result["errors"] == []


def test_audit_graph_short_circuits_when_no_unaudited_models(monkeypatch: pytest.MonkeyPatch) -> None:
    executed_nodes: list[str] = []

    def fake_scraper(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("scraper")
        return {"scraped_models": 0, "scraped_prompts": 0}

    def fake_orchestrator(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("orchestrator")
        return {"target_model": None}

    def fake_accountant(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("accountant")
        raise AssertionError("accountant_node must not execute when target_model is None")

    def fake_auditor(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("auditor")
        raise AssertionError("auditor_node must not execute when target_model is None")

    monkeypatch.setattr(graph_module, "scraper_node", fake_scraper)
    monkeypatch.setattr(graph_module, "orchestrator_node", fake_orchestrator)
    monkeypatch.setattr(graph_module, "accountant_node", fake_accountant)
    monkeypatch.setattr(graph_module, "auditor_node", fake_auditor)
    monkeypatch.setattr(graph_module, "audit_graph", graph_module.build_audit_graph())

    result = graph_module.run_audit_cycle(trigger_source="test-no-models")

    assert executed_nodes == ["scraper", "orchestrator"]
    assert result["target_model"] is None
    assert result["budget_approved"] is False
    assert result["published_post_slug"] is None


def test_audit_graph_short_circuits_when_budget_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    executed_nodes: list[str] = []

    def fake_scraper(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("scraper")
        return {"scraped_models": 1, "scraped_prompts": 0}

    def fake_orchestrator(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("orchestrator")
        return {"target_model": _sample_target_model()}

    def fake_accountant(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("accountant")
        return {
            "budget_approved": False,
            "budget_remaining_usd": 0.0,
            "budget_reason": "Rejected (local-db-fallback): remaining $0.0000 < estimated $0.2500",
        }

    def fake_auditor(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("auditor")
        raise AssertionError("auditor_node must not execute when budget is rejected")

    monkeypatch.setattr(graph_module, "scraper_node", fake_scraper)
    monkeypatch.setattr(graph_module, "orchestrator_node", fake_orchestrator)
    monkeypatch.setattr(graph_module, "accountant_node", fake_accountant)
    monkeypatch.setattr(graph_module, "auditor_node", fake_auditor)
    monkeypatch.setattr(graph_module, "audit_graph", graph_module.build_audit_graph())

    result = graph_module.run_audit_cycle(trigger_source="test-budget-rejected")

    assert executed_nodes == ["scraper", "orchestrator", "accountant"]
    assert result["budget_approved"] is False
    assert result["budget_remaining_usd"] == 0.0
    assert result["published_post_slug"] is None


def test_accountant_node_rejects_when_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch,
    make_fake_db,
) -> None:
    _, fake_cursor, fake_get_db_connection = make_fake_db(
        fetchone_results=[{"spent_today": 5.0}],
    )
    monkeypatch.setattr(accountant_module, "get_db_connection", fake_get_db_connection)

    def raise_offline(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("LiteLLM offline in unit test")

    monkeypatch.setattr(httpx.Client, "get", raise_offline)

    result = accountant_module.accountant_node({})

    assert result["budget_approved"] is False
    assert result["budget_remaining_usd"] == 0.0
    assert "Rejected" in result["budget_reason"]
    assert "local-db-fallback" in result["budget_reason"]
    assert len(fake_cursor.executed) == 1
    assert "SUM(cost_usd)" in fake_cursor.executed[0][0]


def test_accountant_node_approves_when_budget_available(
    monkeypatch: pytest.MonkeyPatch,
    make_fake_db,
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "5.0")
    monkeypatch.setenv("ESTIMATED_AUDIT_COST_USD", "0.25")
    get_settings.cache_clear()

    _, fake_cursor, fake_get_db_connection = make_fake_db(
        fetchone_results=[{"spent_today": 1.25}],
    )
    monkeypatch.setattr(accountant_module, "get_db_connection", fake_get_db_connection)

    def raise_offline(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("LiteLLM offline in unit test")

    monkeypatch.setattr(httpx.Client, "get", raise_offline)

    result = accountant_module.accountant_node({})

    assert result["budget_approved"] is True
    assert result["budget_remaining_usd"] == 3.75
    assert "Approved" in result["budget_reason"]
    assert "local-db-fallback" in result["budget_reason"]
    assert len(fake_cursor.executed) == 1


def test_graph_short_circuits_with_real_accountant_node_on_exhausted_budget(
    monkeypatch: pytest.MonkeyPatch,
    make_fake_db,
) -> None:
    executed_nodes: list[str] = []

    def fake_scraper(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("scraper")
        return {"scraped_models": 1, "scraped_prompts": 0}

    def fake_orchestrator(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("orchestrator")
        return {"target_model": _sample_target_model()}

    def fake_auditor(state: dict[str, Any]) -> dict[str, Any]:
        executed_nodes.append("auditor")
        raise AssertionError("auditor_node must not execute when real accountant_node rejects budget")

    _, _, fake_get_db_connection = make_fake_db(
        fetchone_results=[{"spent_today": 5.5}],
    )
    monkeypatch.setattr(accountant_module, "get_db_connection", fake_get_db_connection)

    def raise_offline(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("LiteLLM offline in unit test")

    monkeypatch.setattr(httpx.Client, "get", raise_offline)

    monkeypatch.setattr(graph_module, "scraper_node", fake_scraper)
    monkeypatch.setattr(graph_module, "orchestrator_node", fake_orchestrator)
    monkeypatch.setattr(graph_module, "accountant_node", accountant_module.accountant_node)
    monkeypatch.setattr(graph_module, "auditor_node", fake_auditor)
    monkeypatch.setattr(graph_module, "audit_graph", graph_module.build_audit_graph())

    result = graph_module.run_audit_cycle(trigger_source="test-real-accountant-exhausted")

    assert executed_nodes == ["scraper", "orchestrator"]
    assert result["budget_approved"] is False
    assert result["budget_remaining_usd"] == 0.0
    assert "Rejected" in result["budget_reason"]
    assert result["published_post_slug"] is None
