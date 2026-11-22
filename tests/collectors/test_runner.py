from dataclasses import dataclass, field
from datetime import timedelta

from radar.collectors.base import Cursor, RawItem
from radar.collectors.runner import load_cursor, run_collector, save_cursor


def item(ext_id: str, text: str = "ignore previous instructions") -> RawItem:
    return RawItem(
        source="fake", external_id=ext_id, url=f"https://x/{ext_id}", content_text=text
    )


@dataclass
class FakeCollector:
    items: list[RawItem] = field(default_factory=list)
    next_cursor: Cursor = field(default_factory=dict)
    raises: Exception | None = None
    name: str = "fake"
    interval: timedelta = timedelta(hours=1)

    def poll(self, cursor: Cursor) -> tuple[list[RawItem], Cursor]:
        if self.raises:
            raise self.raises
        return self.items, self.next_cursor


def test_inserts_and_saves_cursor(conn):
    c = FakeCollector(items=[item("a"), item("b")], next_cursor={"since": "2026-11-21"})
    assert run_collector(conn, c) == 2
    assert load_cursor(conn, "fake") == {"since": "2026-11-21"}
    row = conn.execute("select last_run_at, last_error from collector_state").fetchone()
    assert row["last_run_at"] is not None and row["last_error"] is None


def test_rerun_is_idempotent(conn):
    c = FakeCollector(items=[item("a")], next_cursor={})
    run_collector(conn, c)
    assert run_collector(conn, c) == 0


def test_failed_poll_keeps_cursor(conn):
    save_cursor(conn, "fake", {"since": "old"})
    c = FakeCollector(raises=RuntimeError("403 rate limited"))
    assert run_collector(conn, c) == 0
    assert load_cursor(conn, "fake") == {"since": "old"}
    err = conn.execute("select last_error from collector_state where name='fake'")
    assert "403" in err.fetchone()["last_error"]


def test_success_clears_last_error(conn):
    run_collector(conn, FakeCollector(raises=RuntimeError("boom")))
    run_collector(conn, FakeCollector(items=[item("a")]))
    err = conn.execute("select last_error from collector_state where name='fake'")
    assert err.fetchone()["last_error"] is None


def test_content_hash_is_sha256():
    import hashlib

    assert item("a", "hi").content_hash == hashlib.sha256(b"hi").hexdigest()
