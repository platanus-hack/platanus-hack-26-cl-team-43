import logging
import time
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.accountant import accountant_node
from app.agents.auditor import auditor_node
from app.agents.orchestrator import orchestrator_node
from app.agents.scraper import scraper_node
from app.agents.state import AuditGraphState
from app.services.audit_runs import AuditRunRepository, audit_cycle_lock

logger = logging.getLogger(__name__)
audit_run_repository = AuditRunRepository()


def _route_after_orchestrator(state: AuditGraphState) -> str:
    if state.get("target_model") is None:
        return END
    return "accountant"


def _route_after_accountant(state: AuditGraphState) -> str:
    if not state.get("budget_approved", False):
        return END
    return "auditor"


def build_audit_graph():
    graph = StateGraph(AuditGraphState)

    graph.add_node("scraper", scraper_node)
    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("accountant", accountant_node)
    graph.add_node("auditor", auditor_node)

    graph.add_edge(START, "scraper")
    graph.add_edge("scraper", "orchestrator")
    graph.add_conditional_edges(
        "orchestrator",
        _route_after_orchestrator,
        {"accountant": "accountant", END: END},
    )
    graph.add_conditional_edges(
        "accountant",
        _route_after_accountant,
        {"auditor": "auditor", END: END},
    )
    graph.add_edge("auditor", END)

    return graph.compile()


audit_graph = build_audit_graph()


def run_audit_cycle(trigger_source: str = "scheduler") -> dict[str, Any]:
    initial_state: AuditGraphState = {
        "trigger_source": trigger_source,
        "scraped_models": 0,
        "scraped_prompts": 0,
        "target_model": None,
        "budget_approved": False,
        "budget_remaining_usd": 0.0,
        "budget_reason": "",
        "eval_results": [],
        "published_post_slug": None,
        "errors": [],
    }

    with audit_cycle_lock() as acquired:
        if not acquired:
            return {**initial_state, "errors": ["Audit cycle already in progress"]}

        started_at = time.monotonic()
        run_id: int | None = None
        try:
            run_id = audit_run_repository.create_run(trigger_source=trigger_source)
        except Exception as exc:
            logger.warning("Failed to create audit run record: %s", exc)
            run_id = None

        try:
            result = audit_graph.invoke(initial_state)
        except Exception as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            if run_id is not None:
                try:
                    audit_run_repository.fail_run(
                        run_id,
                        error_summary=str(exc),
                        duration_ms=duration_ms,
                    )
                except Exception as repo_exc:
                    logger.warning("Failed to mark audit run %s as failed: %s", run_id, repo_exc)
            raise

        duration_ms = int((time.monotonic() - started_at) * 1000)
        target_model = result.get("target_model")
        model_id = target_model.get("id") if isinstance(target_model, dict) else None
        budget_approved = bool(result.get("budget_approved", False))
        evaluations_count = len(result.get("eval_results") or [])
        published_post_slug = result.get("published_post_slug")
        error_summary = "; ".join(str(e) for e in (result.get("errors") or []))

        if run_id is not None:
            try:
                audit_run_repository.complete_run(
                    run_id,
                    model_id=model_id,
                    budget_approved=budget_approved,
                    evaluations_count=evaluations_count,
                    published_post_slug=published_post_slug,
                    error_summary=error_summary,
                    duration_ms=duration_ms,
                )
            except Exception as repo_exc:
                logger.warning("Failed to mark audit run %s as completed: %s", run_id, repo_exc)

        return result
