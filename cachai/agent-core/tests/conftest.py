from contextlib import contextmanager
from typing import Any, Callable, Iterator

import pytest

from app.config import get_settings


class FakeCursor:
    """In-memory DB cursor double for offline unit and integration tests."""

    def __init__(
        self,
        fetchone_results: list[Any] | None = None,
        fetchall_results: list[list[Any]] | None = None,
        handler: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._fetchone_results: list[Any] = list(fetchone_results) if fetchone_results is not None else []
        self._fetchall_results: list[list[Any]] = list(fetchall_results) if fetchall_results is not None else []
        self._handler = handler
        self._has_handler_result = False
        self._last_handler_result: Any = None
        self.rowcount: int = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        self.executed.append((query, params))
        if self._handler is not None:
            self._last_handler_result = self._handler(query, params)
            self._has_handler_result = True
        else:
            self._has_handler_result = False

    def fetchone(self) -> Any:
        if self._has_handler_result:
            self._has_handler_result = False
            return self._last_handler_result
        if self._fetchone_results:
            return self._fetchone_results.pop(0)
        return None

    def fetchall(self) -> list[Any]:
        if self._has_handler_result:
            self._has_handler_result = False
            return self._last_handler_result if self._last_handler_result is not None else []
        if self._fetchall_results:
            return self._fetchall_results.pop(0)
        return []


class FakeConnection:
    """In-memory DB connection double supporting context manager semantics."""

    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.commit_count: int = 0
        self.rollback_count: int = 0

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


@pytest.fixture
def make_fake_db() -> Callable[..., tuple[FakeConnection, FakeCursor, Callable[[], Iterator[FakeConnection]]]]:
    """Factory returning (fake_conn, fake_cursor, fake_get_db_connection)."""

    def _factory(
        fetchone_results: list[Any] | None = None,
        fetchall_results: list[list[Any]] | None = None,
        handler: Callable[[str, Any], Any] | None = None,
    ) -> tuple[FakeConnection, FakeCursor, Callable[[], Iterator[FakeConnection]]]:
        fake_cursor = FakeCursor(
            fetchone_results=fetchone_results,
            fetchall_results=fetchall_results,
            handler=handler,
        )
        fake_conn = FakeConnection(fake_cursor)

        @contextmanager
        def fake_get_db_connection() -> Iterator[FakeConnection]:
            yield fake_conn

        return fake_conn, fake_cursor, fake_get_db_connection

    return _factory


@pytest.fixture(autouse=True)
def isolated_test_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide deterministic test settings and reset lru_cache around every test."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/cachai_test")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test-master-key")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-internal-token")
    monkeypatch.setenv("DAILY_BUDGET_USD", "5.0")
    monkeypatch.setenv("ESTIMATED_AUDIT_COST_USD", "0.25")
    monkeypatch.setenv("DEMO_MODE", "true")

    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()
