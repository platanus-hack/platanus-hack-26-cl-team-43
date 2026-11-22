from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
import logging
import os
import re
import time
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import httpx

logger = logging.getLogger(__name__)

_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

DEFAULT_OPENAI_FEED_URL = "https://openai.com/news/rss.xml"
DEFAULT_ANTHROPIC_FEED_URL = "https://www.anthropic.com/news"
DEFAULT_HUGGINGFACE_FEED_URL = "https://huggingface.co/blog/feed.xml"
DEFAULT_MIN_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class ReleaseEntry:
    model_name: str
    provider: str
    release_date: date
    source_url: str = ""


@dataclass(frozen=True)
class FeedSource:
    provider: str
    url: str
    feed_format: str
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS


MVP_SEED_RELEASES: tuple[ReleaseEntry, ...] = (
    ReleaseEntry("gpt-4o-mini", "openai", date(2025, 1, 15), "seed://mvp"),
    ReleaseEntry("claude-3-5-haiku", "anthropic", date(2025, 2, 1), "seed://mvp"),
    ReleaseEntry("llama-3.1-8b-instruct", "meta", date(2025, 2, 10), "seed://mvp"),
    ReleaseEntry("mistral-small-2501", "mistral", date(2025, 1, 30), "seed://mvp"),
)


def _read_min_interval_seconds() -> float:
    raw = os.getenv("SCRAPER_MIN_INTERVAL_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_MIN_INTERVAL_SECONDS
    try:
        return float(raw.strip())
    except ValueError:
        logger.warning(
            "Invalid SCRAPER_MIN_INTERVAL_SECONDS=%r; falling back to %.1fs.",
            raw,
            DEFAULT_MIN_INTERVAL_SECONDS,
        )
        return DEFAULT_MIN_INTERVAL_SECONDS


def get_default_feed_sources() -> tuple[FeedSource, ...]:
    """Return configurable default feed sources for OpenAI, Anthropic, and HuggingFace."""
    min_interval = _read_min_interval_seconds()
    return (
        FeedSource(
            provider="openai",
            url=os.getenv("OPENAI_FEED_URL", DEFAULT_OPENAI_FEED_URL),
            feed_format="rss",
            min_interval_seconds=min_interval,
        ),
        FeedSource(
            provider="anthropic",
            url=os.getenv("ANTHROPIC_FEED_URL", DEFAULT_ANTHROPIC_FEED_URL),
            feed_format="html",
            min_interval_seconds=min_interval,
        ),
        FeedSource(
            provider="huggingface",
            url=os.getenv("HUGGINGFACE_FEED_URL", DEFAULT_HUGGINGFACE_FEED_URL),
            feed_format="rss",
            min_interval_seconds=min_interval,
        ),
    )


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _parse_date(raw: str | None, fallback: date) -> date:
    if not raw:
        return fallback
    text = raw.strip()
    if not text:
        return fallback

    # Try RFC 2822 first (standard for RSS 2.0 <pubDate>).
    try:
        parsed_rfc = parsedate_to_datetime(text)
        if parsed_rfc is not None:
            return parsed_rfc.date()
    except (TypeError, ValueError):
        pass

    # Try ISO-8601 datetime or date (standard for Atom and HTML <time datetime>).
    normalized_iso = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized_iso).date()
    except ValueError:
        pass

    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    match = _ISO_DATE_RE.search(text)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            pass

    return fallback


def _extract_entry_link(node: ET.Element, fallback_url: str) -> str:
    first_href = ""
    for child in node:
        if _local_name(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        if href:
            rel = (child.attrib.get("rel") or "").strip().lower()
            if rel in ("", "alternate"):
                return href
            if not first_href:
                first_href = href
        text_link = (child.text or "").strip()
        if text_link:
            return text_link
    return first_href or fallback_url


def parse_rss_feed(
    xml_text: str,
    provider: str,
    source_url: str = "",
    default_date: date | None = None,
) -> list[ReleaseEntry]:
    """Parse an RSS 2.0 or Atom XML feed into ReleaseEntry items."""
    root = ET.fromstring(xml_text)
    normalized_provider = provider.strip().lower()
    fallback_date = default_date or date.today()
    entries: list[ReleaseEntry] = []

    for node in root.iter():
        tag_name = _local_name(node.tag)
        if tag_name not in ("item", "entry"):
            continue

        title = ""
        pub_date_raw: str | None = None
        updated_raw: str | None = None

        for child in node:
            child_name = _local_name(child.tag)
            if child_name == "title" and not title:
                title = (child.text or "").strip()
            elif child_name in ("pubDate", "published") and pub_date_raw is None:
                pub_date_raw = child.text
            elif child_name == "updated" and updated_raw is None:
                updated_raw = child.text

        if not title:
            continue

        release_date = _parse_date(pub_date_raw or updated_raw, fallback_date)
        entry_url = _extract_entry_link(node, source_url)
        entries.append(
            ReleaseEntry(
                model_name=title,
                provider=normalized_provider,
                release_date=release_date,
                source_url=entry_url,
            )
        )

    return entries


def parse_html_feed(
    html_text: str,
    provider: str,
    source_url: str = "",
    default_date: date | None = None,
) -> list[ReleaseEntry]:
    """Parse an HTML news/changelog page into ReleaseEntry items."""
    soup = BeautifulSoup(html_text, "html.parser")
    normalized_provider = provider.strip().lower()
    fallback_date = default_date or date.today()
    entries: list[ReleaseEntry] = []

    articles = soup.find_all("article")
    if articles:
        for article in articles:
            heading = article.select_one("h1, h2, h3")
            title = heading.get_text(strip=True) if heading else ""
            if not title:
                continue

            time_tag = article.find("time")
            raw_date: str | None = None
            if time_tag is not None:
                raw_date = time_tag.get("datetime") or time_tag.get_text(strip=True)
            release_date = _parse_date(raw_date, fallback_date)

            link_tag = article.find("a", href=True)
            entry_url = source_url
            if link_tag is not None:
                href = str(link_tag["href"]).strip()
                if href:
                    entry_url = urljoin(source_url, href) if source_url else href

            entries.append(
                ReleaseEntry(
                    model_name=title,
                    provider=normalized_provider,
                    release_date=release_date,
                    source_url=entry_url,
                )
            )
        return entries

    for heading in soup.select("h2, h3"):
        title = heading.get_text(strip=True)
        if not title:
            continue

        entry_url = source_url
        link_tag = heading.find("a", href=True)
        if link_tag is None and heading.parent is not None and heading.parent.name == "a":
            if heading.parent.has_attr("href"):
                link_tag = heading.parent
        if link_tag is not None:
            href = str(link_tag["href"]).strip()
            if href:
                entry_url = urljoin(source_url, href) if source_url else href

        entries.append(
            ReleaseEntry(
                model_name=title,
                provider=normalized_provider,
                release_date=fallback_date,
                source_url=entry_url,
            )
        )

    return entries


def deduplicate_releases(entries: Iterable[ReleaseEntry]) -> list[ReleaseEntry]:
    """Deduplicate releases by normalized (model_name, provider), preserving order."""
    seen: set[tuple[str, str]] = set()
    unique: list[ReleaseEntry] = []
    for entry in entries:
        key = (entry.model_name.strip().lower(), entry.provider.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


class SourceRateLimiter:
    """In-memory per-source minimum-interval rate limiter."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last_fetch: dict[str, float] = {}

    def is_allowed(self, source_key: str, min_interval_seconds: float) -> bool:
        if min_interval_seconds <= 0:
            return True
        last_ts = self._last_fetch.get(source_key)
        if last_ts is None:
            return True
        return (self._clock() - last_ts) >= min_interval_seconds

    def record_fetch(self, source_key: str) -> None:
        self._last_fetch[source_key] = self._clock()

    def try_acquire(self, source_key: str, min_interval_seconds: float) -> bool:
        if not self.is_allowed(source_key, min_interval_seconds):
            return False
        self.record_fetch(source_key)
        return True

    def reset(self) -> None:
        self._last_fetch.clear()


class ReleaseFeedScraper:
    """Fetches, parses, rate-limits, and deduplicates model release feeds."""

    def __init__(
        self,
        sources: Sequence[FeedSource] | None = None,
        client: httpx.Client | None = None,
        rate_limiter: SourceRateLimiter | None = None,
        timeout_seconds: float = 5.0,
        fallback_releases: Sequence[ReleaseEntry] = MVP_SEED_RELEASES,
    ) -> None:
        self.sources: tuple[FeedSource, ...] = (
            tuple(sources) if sources is not None else get_default_feed_sources()
        )
        self._client = client
        self.rate_limiter = rate_limiter if rate_limiter is not None else SourceRateLimiter()
        self.timeout_seconds = timeout_seconds
        self.fallback_releases: tuple[ReleaseEntry, ...] = tuple(fallback_releases)

    def fetch_source(self, source: FeedSource) -> list[ReleaseEntry]:
        """Fetch and parse a single feed source, returning [] on rate-limit or error."""
        source_key = f"{source.provider}:{source.url}"
        if not self.rate_limiter.try_acquire(source_key, source.min_interval_seconds):
            logger.info(
                "Skipping feed %s (rate-limited, min_interval=%.1fs).",
                source_key,
                source.min_interval_seconds,
            )
            return []

        try:
            if self._client is not None:
                resp = self._client.get(source.url)
                resp.raise_for_status()
                body = resp.text
            else:
                with httpx.Client(
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                ) as client:
                    resp = client.get(source.url)
                    resp.raise_for_status()
                    body = resp.text

            if source.feed_format.strip().lower() == "rss":
                return parse_rss_feed(body, provider=source.provider, source_url=source.url)
            return parse_html_feed(body, provider=source.provider, source_url=source.url)
        except httpx.HTTPError as exc:
            logger.warning("HTTP error fetching feed %s: %s", source_key, exc)
            return []
        except ET.ParseError as exc:
            logger.warning("XML parse error for feed %s: %s", source_key, exc)
            return []
        except Exception as exc:
            logger.warning("Unexpected error fetching feed %s: %s", source_key, exc)
            return []

    def fetch_all(self, use_fallback: bool = True) -> list[ReleaseEntry]:
        """Fetch all configured sources, deduplicate, and optionally fall back to seeds."""
        collected: list[ReleaseEntry] = []
        for source in self.sources:
            collected.extend(self.fetch_source(source))

        deduplicated = deduplicate_releases(collected)
        if not deduplicated and use_fallback:
            logger.info("No live feed releases retrieved; using fallback seed catalog.")
            return deduplicate_releases(self.fallback_releases)
        return deduplicated
