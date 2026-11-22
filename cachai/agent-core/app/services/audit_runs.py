from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
import logging
from typing import Any

import app.db as db_module
from app.db import get_db_connection

logger = logging.getLogger(__name__)

AUDIT_CYCLE_LOCK_ID: int = 842901
_ORIG_GET_DB_CONNECTION = db_module.get_db_connection


@contextmanager
def _open_db_connection(timeout: float = 1.0) -> Iterator[Any]:
    if get_db_connection is not _ORIG_GET_DB_CONNECTION:
        with get_db_connection() as conn:
            yield conn
        return

    if db_module.get_db_connection is not _ORIG_GET_DB_CONNECTION:
        with db_module.get_db_connection() as conn:
            yield conn
        return

    pool = db_module.init_db_pool()
    with pool.connection(timeout=timeout) as conn:
        yield conn


def _extract_row_value(row: Any, key: str, index: int = 0) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        if key in row:
            return row[key]
        values = list(row.values())
        return values[index] if index < len(values) else None
    return row[index]


@contextmanager
def audit_cycle_lock(lock_id: int = AUDIT_CYCLE_LOCK_ID) -> Iterator[bool]:
    """Acquire a session-level PostgreSQL advisory lock for an audit cycle.

    Yields ``True`` when the lock is acquired (or when the database is
    unavailable as a safe fallback) and ``False`` when another worker already
    holds the lock.
    """
    acquired = False
    conn: Any = None
    with ExitStack() as stack:
        try:
            conn = stack.enter_context(_open_db_connection())
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (lock_id,))
                row = cur.fetchone()
            acquired = bool(_extract_row_value(row, "acquired", 0))
        except Exception as exc:
            logger.warning(
                "Failed to acquire audit cycle advisory lock %s (%s); continuing without DB lock.",
                lock_id,
                exc,
            )
            yield True
            return

        try:
            yield acquired
        finally:
            if acquired and conn is not None:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
                except Exception as exc:
                    logger.warning(
                        "Failed to release audit cycle advisory lock %s (%s).",
                        lock_id,
                        exc,
                    )


class AuditRunRepository:
    """Persistence layer for audit_runs lifecycle records."""

    def create_run(
        self,
        trigger_source: str = "scheduler",
        model_id: int | None = None,
    ) -> int | None:
        try:
            with _open_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO audit_runs (trigger_source, model_id, status)
                        VALUES (%s, %s, 'running')
                        RETURNING id
                        """,
                        (trigger_source, model_id),
                    )
                    row = cur.fetchone()
                conn.commit()
            run_id = _extract_row_value(row, "id", 0)
            return int(run_id) if run_id is not None else None
        except Exception as exc:
            logger.warning("Failed to create audit_runs record (%s).", exc)
            return None

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
        try:
            with _open_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE audit_runs
                        SET
                            model_id = COALESCE(%s, model_id),
                            status = 'completed',
                            budget_approved = %s,
                            evaluations_count = %s,
                            published_post_slug = %s,
                            error_summary = %s,
                            finished_at = NOW(),
                            duration_ms = %s
                        WHERE id = %s
                        """,
                        (
                            model_id,
                            budget_approved,
                            evaluations_count,
                            published_post_slug,
                            error_summary,
                            duration_ms,
                            run_id,
                        ),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("Failed to complete audit_runs record %s (%s).", run_id, exc)
            return False

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
        try:
            with _open_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE audit_runs
                        SET
                            model_id = COALESCE(%s, model_id),
                            status = 'failed',
                            budget_approved = %s,
                            evaluations_count = %s,
                            error_summary = %s,
                            finished_at = NOW(),
                            duration_ms = %s
                        WHERE id = %s
                        """,
                        (
                            model_id,
                            budget_approved,
                            evaluations_count,
                            error_summary,
                            duration_ms,
                            run_id,
                        ),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("Failed to mark audit_runs record %s as failed (%s).", run_id, exc)
            return False
