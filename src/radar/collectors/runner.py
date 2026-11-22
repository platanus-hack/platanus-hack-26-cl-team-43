import logging

from psycopg.types.json import Jsonb

from radar.collectors.base import Collector, Cursor

log = logging.getLogger(__name__)


def load_cursor(conn, name: str) -> Cursor:
    row = conn.execute(
        "select cursor from collector_state where name = %s", (name,)
    ).fetchone()
    return (row["cursor"] if row else None) or {}


def save_cursor(conn, name: str, cursor: Cursor) -> None:
    _upsert_cursor(conn, name, cursor)
    conn.commit()


def _upsert_cursor(conn, name: str, cursor: Cursor) -> None:
    """Upsert cursor + last_run_at, clearing last_error. Does not commit."""
    conn.execute(
        """
        insert into collector_state (name, cursor, last_run_at, last_error)
        values (%s, %s, now(), null)
        on conflict (name) do update
        set cursor = excluded.cursor, last_run_at = now(), last_error = null
        """,
        (name, Jsonb(cursor)),
    )


def run_collector(conn, collector: Collector) -> int:
    """Poll once; returns number of newly inserted raw_items (0 on failure)."""
    try:
        items, new_cursor = collector.poll(load_cursor(conn, collector.name))
        inserted = 0
        for it in items:
            cur = conn.execute(
                """
                insert into raw_items (source, external_id, url, author, license,
                    published_at, content_text, content_hash, kind)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (source, external_id, content_hash) do nothing
                """,
                (it.source, it.external_id, it.url, it.author, it.license,
                 it.published_at, it.content_text, it.content_hash, it.kind),
            )
            inserted += cur.rowcount
        _upsert_cursor(conn, collector.name, new_cursor)
        conn.commit()
        return inserted
    except Exception as exc:
        conn.rollback()
        log.exception("collector %s failed", collector.name)
        conn.execute(
            """
            insert into collector_state (name, last_error) values (%s, %s)
            on conflict (name) do update set last_error = excluded.last_error
            """,
            (collector.name, str(exc)),
        )
        conn.commit()
        return 0
