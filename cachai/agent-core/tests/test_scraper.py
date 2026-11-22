"""Offline unit and integration tests for the release feed scraper and scraper node."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
import importlib
from pathlib import Path
import sys
import types
from typing import Any

import httpx
import pytest

AGENT_CORE_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_CORE_DIR))

from app.services.feed_scraper import (
    MVP_SEED_RELEASES,
    FeedSource,
    ReleaseEntry,
    ReleaseFeedScraper,
    SourceRateLimiter,
    deduplicate_releases,
    get_default_feed_sources,
    parse_html_feed,
    parse_rss_feed,
)


class FakeClock:
    """Deterministic monotonic clock for rate-limiter tests."""

    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# 1. RSS 2.0 and Atom parsing
# ---------------------------------------------------------------------------


def test_parse_rss_feed_rss2_items() -> None:
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>OpenAI News</title>
        <item>
          <title>gpt-4.1-mini</title>
          <pubDate>Wed, 15 Jan 2025 12:00:00 GMT</pubDate>
          <link>https://openai.com/index/gpt-4-1-mini</link>
        </item>
        <item>
          <title>o3-mini</title>
          <pubDate>Fri, 31 Jan 2025 18:30:00 +0000</pubDate>
          <link>https://openai.com/index/o3-mini</link>
        </item>
      </channel>
    </rss>
    """

    entries = parse_rss_feed(
        rss_xml,
        provider="openai",
        source_url="https://openai.com/news/rss.xml",
        default_date=date(2025, 1, 1),
    )

    assert len(entries) == 2
    assert entries[0] == ReleaseEntry(
        model_name="gpt-4.1-mini",
        provider="openai",
        release_date=date(2025, 1, 15),
        source_url="https://openai.com/index/gpt-4-1-mini",
    )
    assert entries[1] == ReleaseEntry(
        model_name="o3-mini",
        provider="openai",
        release_date=date(2025, 1, 31),
        source_url="https://openai.com/index/o3-mini",
    )


def test_parse_rss_feed_atom_entries() -> None:
    atom_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Anthropic Releases</title>
      <entry>
        <title>claude-3-7-sonnet</title>
        <updated>2025-02-24T16:00:00Z</updated>
        <link rel="alternate" href="https://www.anthropic.com/news/claude-3-7-sonnet" />
      </entry>
    </feed>
    """

    entries = parse_rss_feed(
        atom_xml,
        provider="anthropic",
        source_url="https://www.anthropic.com/news/feed.xml",
        default_date=date(2025, 1, 1),
    )

    assert entries == [
        ReleaseEntry(
            model_name="claude-3-7-sonnet",
            provider="anthropic",
            release_date=date(2025, 2, 24),
            source_url="https://www.anthropic.com/news/claude-3-7-sonnet",
        )
    ]


def test_parse_rss_feed_missing_or_invalid_date_uses_default_date() -> None:
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>model-missing-date</title>
          <link>https://example.com/missing-date</link>
        </item>
        <item>
          <title>model-invalid-date</title>
          <pubDate>not-a-valid-date</pubDate>
        </item>
      </channel>
    </rss>
    """
    fallback_day = date(2025, 3, 10)
    feed_url = "https://example.com/feed.xml"

    entries = parse_rss_feed(
        rss_xml,
        provider="huggingface",
        source_url=feed_url,
        default_date=fallback_day,
    )

    assert len(entries) == 2
    assert entries[0].model_name == "model-missing-date"
    assert entries[0].release_date == fallback_day
    assert entries[0].source_url == "https://example.com/missing-date"

    assert entries[1].model_name == "model-invalid-date"
    assert entries[1].release_date == fallback_day
    assert entries[1].source_url == feed_url


# ---------------------------------------------------------------------------
# 2. HTML parsing
# ---------------------------------------------------------------------------


def test_parse_html_feed_article_blocks() -> None:
    html = """
    <html>
      <body>
        <article>
          <h2><a href="https://www.anthropic.com/news/claude-4">claude-4-sonnet</a></h2>
          <time datetime="2025-05-22T10:00:00Z">May 22, 2025</time>
        </article>
        <article>
          <h3>claude-4-haiku</h3>
          <a href="https://www.anthropic.com/news/claude-4-haiku">Read more</a>
        </article>
      </body>
    </html>
    """
    fallback_day = date(2025, 5, 1)

    entries = parse_html_feed(
        html,
        provider="anthropic",
        source_url="https://www.anthropic.com/news",
        default_date=fallback_day,
    )

    assert len(entries) == 2
    assert entries[0] == ReleaseEntry(
        model_name="claude-4-sonnet",
        provider="anthropic",
        release_date=date(2025, 5, 22),
        source_url="https://www.anthropic.com/news/claude-4",
    )
    assert entries[1] == ReleaseEntry(
        model_name="claude-4-haiku",
        provider="anthropic",
        release_date=fallback_day,
        source_url="https://www.anthropic.com/news/claude-4-haiku",
    )


def test_parse_html_feed_heading_only_fallback() -> None:
    html = """
    <html>
      <body>
        <section>
          <h2>llama-4-scout</h2>
          <h3>llama-4-maverick</h3>
        </section>
      </body>
    </html>
    """
    fallback_day = date(2025, 4, 5)
    source_url = "https://huggingface.co/blog"

    entries = parse_html_feed(
        html,
        provider="meta",
        source_url=source_url,
        default_date=fallback_day,
    )

    assert entries == [
        ReleaseEntry(
            model_name="llama-4-scout",
            provider="meta",
            release_date=fallback_day,
            source_url=source_url,
        ),
        ReleaseEntry(
            model_name="llama-4-maverick",
            provider="meta",
            release_date=fallback_day,
            source_url=source_url,
        ),
    ]


# ---------------------------------------------------------------------------
# 3. Deduplication
# ---------------------------------------------------------------------------


def test_deduplicate_releases_normalizes_and_preserves_order() -> None:
    entries = [
        ReleaseEntry("GPT-4o-mini", "OpenAI", date(2025, 1, 15), "https://first"),
        ReleaseEntry("  gpt-4o-mini ", " openai ", date(2025, 1, 20), "https://duplicate"),
        ReleaseEntry("gpt-4o-mini", "azure", date(2025, 1, 22), "https://different-provider"),
        ReleaseEntry("claude-3-5-haiku", "anthropic", date(2025, 2, 1), "https://anthropic"),
        ReleaseEntry("CLAUDE-3-5-HAIKU", "ANTHROPIC", date(2025, 2, 2), "https://dup-2"),
    ]

    result = deduplicate_releases(entries)

    assert result == [
        ReleaseEntry("GPT-4o-mini", "OpenAI", date(2025, 1, 15), "https://first"),
        ReleaseEntry("gpt-4o-mini", "azure", date(2025, 1, 22), "https://different-provider"),
        ReleaseEntry("claude-3-5-haiku", "anthropic", date(2025, 2, 1), "https://anthropic"),
    ]


# ---------------------------------------------------------------------------
# 4. Per-source rate limiting
# ---------------------------------------------------------------------------


def test_source_rate_limiter_enforces_interval_and_reset() -> None:
    clock = FakeClock(initial=1000.0)
    limiter = SourceRateLimiter(clock=clock)

    assert limiter.is_allowed("openai", 60.0) is True
    assert limiter.try_acquire("openai", 60.0) is True
    assert limiter.is_allowed("openai", 60.0) is False
    assert limiter.try_acquire("openai", 60.0) is False

    # Other sources remain independent.
    assert limiter.is_allowed("anthropic", 60.0) is True

    clock.advance(59.9)
    assert limiter.is_allowed("openai", 60.0) is False

    clock.advance(0.1)
    assert limiter.is_allowed("openai", 60.0) is True
    limiter.record_fetch("openai")
    assert limiter.is_allowed("openai", 60.0) is False

    limiter.reset()
    assert limiter.is_allowed("openai", 60.0) is True


def test_scraper_fetch_source_respects_rate_limiter() -> None:
    clock = FakeClock(initial=500.0)
    limiter = SourceRateLimiter(clock=clock)
    call_count = 0

    rss_payload = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>gpt-4o-mini</title>
          <pubDate>Wed, 15 Jan 2025 00:00:00 GMT</pubDate>
          <link>https://openai.com/index/gpt-4o-mini</link>
        </item>
      </channel>
    </rss>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, text=rss_payload)

    source = FeedSource(
        provider="openai",
        url="https://openai.com/news/rss.xml",
        feed_format="rss",
        min_interval_seconds=120.0,
    )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        scraper = ReleaseFeedScraper(
            sources=(source,),
            client=client,
            rate_limiter=limiter,
        )

        first = scraper.fetch_source(source)
        assert len(first) == 1
        assert call_count == 1

        # Second call before min_interval_seconds skips HTTP request entirely.
        clock.advance(60.0)
        second = scraper.fetch_source(source)
        assert second == []
        assert call_count == 1

        # After min_interval_seconds elapses, HTTP request is allowed again.
        clock.advance(60.0)
        third = scraper.fetch_source(source)
        assert len(third) == 1
        assert call_count == 2


# ---------------------------------------------------------------------------
# 5. Offline / network error fallback
# ---------------------------------------------------------------------------


def test_mvp_seed_releases_contains_four_expected_entries() -> None:
    assert len(MVP_SEED_RELEASES) == 4
    pairs = {(entry.model_name, entry.provider) for entry in MVP_SEED_RELEASES}
    assert pairs == {
        ("gpt-4o-mini", "openai"),
        ("claude-3-5-haiku", "anthropic"),
        ("llama-3.1-8b-instruct", "meta"),
        ("mistral-small-2501", "mistral"),
    }


def test_fetch_all_connect_error_fallback_behavior() -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Network unreachable", request=request)

    source = FeedSource(
        provider="openai",
        url="https://openai.com/news/rss.xml",
        feed_format="rss",
        min_interval_seconds=0.0,
    )

    with httpx.Client(transport=httpx.MockTransport(failing_handler)) as client:
        scraper = ReleaseFeedScraper(
            sources=(source,),
            client=client,
            rate_limiter=SourceRateLimiter(clock=FakeClock()),
        )

        with_fallback = scraper.fetch_all(use_fallback=True)
        assert with_fallback == list(MVP_SEED_RELEASES)

        without_fallback = scraper.fetch_all(use_fallback=False)
        assert without_fallback == []


def test_fetch_all_http_500_fallback_behavior() -> None:
    def server_error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error", request=request)

    source = FeedSource(
        provider="anthropic",
        url="https://www.anthropic.com/news",
        feed_format="html",
        min_interval_seconds=0.0,
    )

    with httpx.Client(transport=httpx.MockTransport(server_error_handler)) as client:
        scraper = ReleaseFeedScraper(
            sources=(source,),
            client=client,
            rate_limiter=SourceRateLimiter(clock=FakeClock()),
        )

        assert scraper.fetch_all(use_fallback=True) == list(MVP_SEED_RELEASES)
        assert scraper.fetch_all(use_fallback=False) == []


# ---------------------------------------------------------------------------
# 6. Configurable feed URLs via environment variables
# ---------------------------------------------------------------------------


def test_get_default_feed_sources_reads_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_FEED_URL", "https://mirror.example.com/openai.xml")
    monkeypatch.setenv("ANTHROPIC_FEED_URL", "https://mirror.example.com/anthropic.html")
    monkeypatch.setenv("HUGGINGFACE_FEED_URL", "https://mirror.example.com/hf.xml")
    monkeypatch.setenv("SCRAPER_MIN_INTERVAL_SECONDS", "45.5")

    sources = get_default_feed_sources()
    by_provider = {src.provider: src for src in sources}

    assert by_provider["openai"].url == "https://mirror.example.com/openai.xml"
    assert by_provider["anthropic"].url == "https://mirror.example.com/anthropic.html"
    assert by_provider["huggingface"].url == "https://mirror.example.com/hf.xml"
    assert all(src.min_interval_seconds == pytest.approx(45.5) for src in sources)


def test_get_default_feed_sources_defaults_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in (
        "OPENAI_FEED_URL",
        "ANTHROPIC_FEED_URL",
        "HUGGINGFACE_FEED_URL",
        "SCRAPER_MIN_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)

    sources = get_default_feed_sources()

    assert len(sources) >= 3
    assert all(src.url.startswith("http") for src in sources)
    assert all(src.min_interval_seconds > 0 for src in sources)


# ---------------------------------------------------------------------------
# 7. scraper_node integration test with mocked DB & feed scraper
# ---------------------------------------------------------------------------


class _FakeDBState:
    """In-memory store simulating unique constraints for scraper_node."""

    def __init__(self) -> None:
        self.models: set[tuple[str, str]] = set()
        self.prompts: set[tuple[str, str]] = set()
        self.executed_sql: list[str] = []
        self.commits: int = 0
        self._next_id: int = 1

    def next_id(self) -> int:
        val = self._next_id
        self._next_id += 1
        return val


class _FakeCursor:
    def __init__(self, db_state: _FakeDBState) -> None:
        self._state = db_state
        self._last_result: tuple[int] | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self._state.executed_sql.append(sql)
        normalized = " ".join(sql.split()).upper()

        if "INSERT INTO MODEL_RELEASES" in normalized:
            assert "ON CONFLICT (MODEL_NAME, PROVIDER) DO NOTHING" in normalized
            key = (str(params[0]), str(params[1]))
            if key in self._state.models:
                self._last_result = None
            else:
                self._state.models.add(key)
                self._last_result = (self._state.next_id(),)
        elif "INSERT INTO EVAL_DATASETS" in normalized:
            assert "ON CONFLICT (CATEGORY, PROMPT_TEXT) DO NOTHING" in normalized
            key = (str(params[0]), str(params[1]))
            if key in self._state.prompts:
                self._last_result = None
            else:
                self._state.prompts.add(key)
                self._last_result = (self._state.next_id(),)
        else:
            raise AssertionError(f"Unexpected SQL executed: {sql}")

    def fetchone(self) -> tuple[int] | None:
        return self._last_result


class _FakeConnection:
    def __init__(self, db_state: _FakeDBState) -> None:
        self._state = db_state

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._state)

    def commit(self) -> None:
        self._state.commits += 1


class _StubFeedScraper:
    def __init__(self, releases: list[ReleaseEntry]) -> None:
        self._releases = releases

    def fetch_all(self, use_fallback: bool = True) -> list[ReleaseEntry]:
        return list(self._releases)


def _load_scraper_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Import app.agents.scraper, stubbing app.db if psycopg is not installed locally."""
    try:
        return importlib.import_module("app.agents.scraper")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] != "psycopg":
            raise
        stub_db = types.ModuleType("app.db")
        stub_db.get_db_connection = None  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "app.db", stub_db)
        monkeypatch.delitem(sys.modules, "app.agents.scraper", raising=False)
        return importlib.import_module("app.agents.scraper")


def test_scraper_node_idempotent_inserts(monkeypatch: pytest.MonkeyPatch) -> None:
    scraper_module = _load_scraper_module(monkeypatch)
    db_state = _FakeDBState()
    stub_releases = [
        ReleaseEntry(
            model_name="gpt-4o-mini",
            provider="openai",
            release_date=date(2025, 1, 15),
            source_url="https://openai.com/index/gpt-4o-mini",
        ),
        ReleaseEntry(
            model_name="claude-3-5-haiku",
            provider="anthropic",
            release_date=date(2025, 2, 1),
            source_url="https://www.anthropic.com/news/claude-3-5-haiku",
        ),
    ]

    @contextmanager
    def fake_get_db_connection() -> Iterator[_FakeConnection]:
        yield _FakeConnection(db_state)

    monkeypatch.setattr(scraper_module, "get_db_connection", fake_get_db_connection)
    monkeypatch.setattr(
        scraper_module,
        "get_feed_scraper",
        lambda: _StubFeedScraper(stub_releases),
    )

    first_run = scraper_module.scraper_node({"trigger_source": "test-run-1"})
    assert first_run["scraped_models"] == 2
    assert first_run["scraped_prompts"] >= 1
    assert db_state.commits == 1
    assert db_state.models == {
        ("gpt-4o-mini", "openai"),
        ("claude-3-5-haiku", "anthropic"),
    }

    # Re-running with the same releases and prompts must be idempotent (0 new rows).
    second_run = scraper_module.scraper_node({"trigger_source": "test-run-2"})
    assert second_run["scraped_models"] == 0
    assert second_run["scraped_prompts"] == 0
    assert db_state.commits == 2
