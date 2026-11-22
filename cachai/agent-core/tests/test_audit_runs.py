from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from app.agents import graph as graph_module
from app.services import audit_runs as audit_runs_module
from app.services.audit_runs import AUDIT_CYCLE_LOCK_ID, AuditRunRepository, audit_cycle_lock


def _param_values(params: Any) -> list[Any]:
    if isinstance(params, dict):
        return list(params.values())
    if isinstance(params, (list, tuple)):
        return list(params)
    return [params]


def test_audit_cycle_lock_acquires_and_unlocks_on_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
    make_fake_db,
) -> None:
    _, fake_cursor, fake_get_db_connection = make_fake_db(
        fetchone_results=[{"acquired": True}],
    )
    monkeypatch.setattr(audit_runs_module, "get_db_connection", fake_get_db_connection)

    body_ran = False
    with audit_cycle_lock() as acquired:
        assert acquired is True
        body_ran = True

    assert body_ran is True
    assert len(fake_cursor.executed) == 2

    lock_sql, lock_params = fake_cursor.executed[0]
    unlock_sql, unlock_params = fake_cursor.executed[1]
    assert "pg_try_advisory_lock" in lock_sql
    assert AUDIT_CYCLE_LOCK_ID in _param_values(lock_params)
    assert "pg_advisory_unlock" in unlock_sql
    assert AUDIT_CYCLE_LOCK_ID in _param_values(unlock_params)


def test_audit_cycle_lock_unlocks_when_body_raises_exception(
    monkeypatch: pytest.MonkeyPatch,
    make_fake_db,
) -> None:
    custom_lock_id = 999111
    _, fake_cursor, fake_get_db_connection = make_fake_db(
        fetchone_results=[{"acquired": True}],
    )
    monkeypatch.setattr(audit_runs_module, "get_db_connection", fake_get_db_connection)

    with pytest.raises(RuntimeError, match="boom inside lock"):
        with audit_cycle_lock(lock_id=custom_lock_id) as acquired:
            assert acquired is True
            raise RuntimeError("boom inside lock")

    assert len(fake_cursor.executed) == 2
    assert "pg_try_advisory_lock" in fake_cursor.executed[0][0]
    assert custom_lock_id in _param_values(fake_cursor.executed[0][1])
    assert "pg_advisory_unlock" in fake_cursor.executed[1][0]
    assert custom_lock_id in _param_values(fake_cursor.executed[1][1])


def test_audit_cycle_lock_contention_yields_false_without_unlock(
    monkeypatch: pytest.MonkeyPatch,
    make_fake_db,
) -> None:
    _, fake_cursor, fake_get_db_connection = make_fake_db(
        fetchone_results=[{"acquired": False}],
    )
    monkeypatch.setattr(audit_runs_module, "get_db_connection", fake_get_db_connection)

    with audit_cycle_lock() as acquired:
        assert acquired is False

    assert len(fake_cursor.executed) == 1
    assert "pg_try_advisory_lock" in fake_cursor.executed[0][0]
    assert all("pg_advisory_unlock" not in sql for sql, _ in fake_cursor.executed)


def test_audit_cycle_lock_falls_back_to_true_when_db_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def failing_db_connection() -> Iterator[Any]:
        raise RuntimeError("database connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(audit_runs_module, "get_db_connection", failing_db_connection)

    entered = False
    with audit_cycle_lock() as acquired:
        assert acquired is True
        entered = True

    assert entered is True


def test_audit_run_repository_create_run_persists_and_commits(
    monkeypatch: pytest.MonkeyPatch,
    make_fake_db,
) -> None:
    fake_conn, fake_cursor, fake_get_db_connection = make_fake_db(
        fetchone_results=[{"id": 42}],
    )
    monkeypatch.setattr(audit_runs_module, "get_db_connection", fake_get_db_connection)

    repo = AuditRunRepository()
    run_id = repo.create_run(trigger_source="manual", model_id=7)

    assert run_id == 42
    assert fake_conn.commit_count == 1
    assert len(fake_cursor.executed) == 1
    sql, params = fake_cursor.executed[0]
    assert "INSERT INTO audit_runs" in sql
    values = _param_values(params)
    assert "manual" in values
    assert 7 in values


def test_audit_run_repository_complete_run_updates_and_commits(
    monkeypatch: pytest.MonkeyPatch,
    make_fake_db,
) -> None:
    fake_conn, fake_cursor, fake_get_db_connection = make_fake_db(
        fetchone_results=[{"id": 42}],
    )
    fake_cursor.rowcount = 1
    monkeypatch.setattr(audit_runs_module, "get_db_connection", fake_get_db_connection)

    repo = AuditRunRepository()
    updated = repo.complete_run(
        42,
        model_id=7,
        budget_approved=True,
        evaluations_count=5,
        published_post_slug="audit-openai-gpt-4o-mini",
        error_summary="",
        duration_ms=1234,
    )

    assert updated is True
    assert fake_conn.commit_count == 1
    assert len(fake_cursor.executed) == 1
    sql, params = fake_cursor.executed[0]
    assert "UPDATE audit_runs" in sql
    values = _param_values(params)
    assert 42 in values
    assert 7 in values
    assert True in values
    assert 5 in values
    assert "audit-openai-gpt-4o-mini" in values
    assert 1234 in values


def test_audit_run_repository_fail_run_updates_and_commits(
    monkeypatch: pytest.MonkeyPatch,
    make_fake_db,
) -> None:
    fake_conn, fake_cursor, fake_get_db_connection = make_fake_db(
        fetchone_results=[{"id": 42}],
    )
    fake_cursor.rowcount = 1
    monkeypatch.setattr(audit_runs_module, "get_db_connection", fake_get_db_connection)

    repo = AuditRunRepository()
    updated = repo.fail_run(
        42,
        error_summary="litellm timeout",
        model_id=7,
        budget_approved=False,
        evaluations_count=2,
        duration_ms=980,
    )

    assert updated is True
    assert fake_conn.commit_count == 1
    assert len(fake_cursor.executed) == 1
    sql, params = fake_cursor.executed[0]
    assert "UPDATE audit_runs" in sql
    values = _param_values(params)
    assert 42 in values
    assert "litellm timeout" in values
    assert 7 in values
    assert 2 in values
    assert 980 in values


def test_audit_run_repository_returns_gracefully_on_db_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def failing_db_connection() -> Iterator[Any]:
        raise RuntimeError("database offline")
        yield  # pragma: no cover

    monkeypatch.setattr(audit_runs_module, "get_db_connection", failing_db_connection)

    repo = AuditRunRepository()
    assert repo.create_run(trigger_source="scheduler") is None
    assert repo.complete_run(1, budget_approved=True, evaluations_count=1, duration_ms=50) is False
    assert repo.fail_run(1, error_summary="boom", duration_ms=50) is False


def test_run_audit_cycle_skips_when_lock_not_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def contended_lock() -> Iterator[bool]:
        yield False

    class ForbiddenGraph:
        def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("audit_graph.invoke must not be called when lock is not acquired")

    class ForbiddenRepo:
        def create_run(self, *args: Any, **kwargs: Any) -> int | None:
            raise AssertionError("create_run must not be called when lock is not acquired")

    monkeypatch.setattr(graph_module, "audit_cycle_lock", contended_lock)
    monkeypatch.setattr(graph_module, "audit_graph", ForbiddenGraph())
    monkeypatch.setattr(graph_module, "audit_run_repository", ForbiddenRepo())

    result = graph_module.run_audit_cycle(trigger_source="manual")

    assert result["trigger_source"] == "manual"
    assert result["errors"] == ["Audit cycle already in progress"]
    assert result["target_model"] is None
    assert result["published_post_slug"] is None


def test_run_audit_cycle_persists_lifecycle_on_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def acquired_lock() -> Iterator[bool]:
        yield True

    recorded_calls: list[tuple[str, dict[str, Any]]] = []

    class RecordingRepo:
        def create_run(self, trigger_source: str = "scheduler", model_id: int | None = None) -> int | None:
            recorded_calls.append(("create_run", {"trigger_source": trigger_source, "model_id": model_id}))
            return 101

        def complete_run(
            self,
            run_id: int,
            *,
            model_id: int | None = None,
            budget_approved: bool = False,
            evaluations_count: int = 0,
            published_post_slug: str | None = None,
            error_summary: str = "",
            duration_ms: int | None = None,
        ) -> bool:
            recorded_calls.append(
                (
                    "complete_run",
                    {
                        "run_id": run_id,
                        "model_id": model_id,
                        "budget_approved": budget_approved,
                        "evaluations_count": evaluations_count,
                        "published_post_slug": published_post_slug,
                        "error_summary": error_summary,
                        "duration_ms": duration_ms,
                    },
                )
            )
            return True

        def fail_run(self, *args: Any, **kwargs: Any) -> bool:
            raise AssertionError("fail_run must not be called on happy path")

    class FakeGraph:
        def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
            return {
                **state,
                "target_model": {"id": 9, "model_name": "claude-3-5-sonnet", "provider": "anthropic"},
                "budget_approved": True,
                "eval_results": [
                    {"eval_id": 1, "passed": True},
                    {"eval_id": 2, "passed": False},
                ],
                "published_post_slug": "audit-anthropic-claude-3-5-sonnet",
                "errors": [],
            }

    monkeypatch.setattr(graph_module, "audit_cycle_lock", acquired_lock)
    monkeypatch.setattr(graph_module, "audit_run_repository", RecordingRepo())
    monkeypatch.setattr(graph_module, "audit_graph", FakeGraph())

    result = graph_module.run_audit_cycle(trigger_source="api-trigger")

    assert result["published_post_slug"] == "audit-anthropic-claude-3-5-sonnet"
    assert [name for name, _ in recorded_calls] == ["create_run", "complete_run"]
    assert recorded_calls[0][1] == {"trigger_source": "api-trigger", "model_id": None}

    complete_kwargs = recorded_calls[1][1]
    assert complete_kwargs["run_id"] == 101
    assert complete_kwargs["model_id"] == 9
    assert complete_kwargs["budget_approved"] is True
    assert complete_kwargs["evaluations_count"] == 2
    assert complete_kwargs["published_post_slug"] == "audit-anthropic-claude-3-5-sonnet"
    assert complete_kwargs["error_summary"] == ""
    assert isinstance(complete_kwargs["duration_ms"], int)
    assert complete_kwargs["duration_ms"] >= 0


def test_run_audit_cycle_marks_failed_and_reraises_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def acquired_lock() -> Iterator[bool]:
        yield True

    recorded_calls: list[tuple[str, dict[str, Any]]] = []

    class RecordingRepo:
        def create_run(self, trigger_source: str = "scheduler", model_id: int | None = None) -> int | None:
            recorded_calls.append(("create_run", {"trigger_source": trigger_source, "model_id": model_id}))
            return 202

        def complete_run(self, *args: Any, **kwargs: Any) -> bool:
            raise AssertionError("complete_run must not be called when graph raises")

        def fail_run(
            self,
            run_id: int,
            error_summary: str,
            *,
            model_id: int | None = None,
            budget_approved: bool = False,
            evaluations_count: int = 0,
            duration_ms: int | None = None,
        ) -> bool:
            recorded_calls.append(
                (
                    "fail_run",
                    {
                        "run_id": run_id,
                        "error_summary": error_summary,
                        "duration_ms": duration_ms,
                    },
                )
            )
            return True

    class CrashingGraph:
        def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("auditor node exploded")

    monkeypatch.setattr(graph_module, "audit_cycle_lock", acquired_lock)
    monkeypatch.setattr(graph_module, "audit_run_repository", RecordingRepo())
    monkeypatch.setattr(graph_module, "audit_graph", CrashingGraph())

    with pytest.raises(RuntimeError, match="auditor node exploded"):
        graph_module.run_audit_cycle(trigger_source="scheduler")

    assert [name for name, _ in recorded_calls] == ["create_run", "fail_run"]
    assert recorded_calls[0][1] == {"trigger_source": "scheduler", "model_id": None}
    assert recorded_calls[1][1]["run_id"] == 202
    assert recorded_calls[1][1]["error_summary"] == "auditor node exploded"
    assert isinstance(recorded_calls[1][1]["duration_ms"], int)
    assert recorded_calls[1][1]["duration_ms"] >= 0
