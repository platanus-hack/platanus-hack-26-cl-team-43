import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any

import httpx

from radar.collectors.base import Cursor, RawItem

ARXIV_URL = "http://export.arxiv.org/api/query"
HF_URL = "https://huggingface.co/api/daily_papers"
QUERY = 'abs:jailbreak AND (abs:LLM OR abs:"language model")'
SEEN_CAP = 2000
ATOM = "{http://www.w3.org/2005/Atom}"
GITHUB_RE = re.compile(r"github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)")
VERSION_RE = re.compile(r"v\d+$")


def github_repos(*texts: str | None) -> list[str]:
    found: list[str] = []
    for text in texts:
        for owner, repo in GITHUB_RE.findall(text or ""):
            repo = repo.removesuffix(".git").rstrip(".")
            full = f"{owner}/{repo}"
            if repo and full not in found:
                found.append(full)
    return found


def _ws(s: str | None) -> str:
    return " ".join((s or "").split())


def _dt(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


class Papers:
    name = "papers"
    interval = timedelta(hours=6)

    def __init__(self, conn, http: httpx.Client):
        self.conn = conn
        self.http = http

    def poll(self, cursor: Cursor) -> tuple[list[RawItem], Cursor]:
        seen: list[str] = list(cursor.get("seen", []))
        known = set(seen)
        candidates = self._arxiv() + self._hf()
        items: list[RawItem] = []
        for item, repos in candidates:
            key = f"{item.source}:{item.external_id}"
            if key in known:
                continue
            known.add(key)
            seen.append(key)
            items.append(item)
            for full in repos:
                self.conn.execute(
                    "insert into watch_repos (full_name, status) values (%s, 'from_paper') "
                    "on conflict do nothing", (full,))
        return items, {"seen": seen[-SEEN_CAP:]}

    def _arxiv(self) -> list[tuple[RawItem, list[str]]]:
        resp = self.http.get(ARXIV_URL, params={
            "search_query": QUERY, "sortBy": "submittedDate", "sortOrder": "descending",
            "max_results": 50})
        if resp.status_code != 200:
            raise RuntimeError(f"arxiv returned {resp.status_code}")
        out = []
        for e in ET.fromstring(resp.text).findall(f"{ATOM}entry"):
            url = _ws(e.findtext(f"{ATOM}id"))
            url = VERSION_RE.sub("", url)
            arxiv_id = url.rsplit("/abs/", 1)[-1]
            title = _ws(e.findtext(f"{ATOM}title"))
            summary = _ws(e.findtext(f"{ATOM}summary"))
            authors = ", ".join(_ws(a.findtext(f"{ATOM}name")) for a in e.findall(f"{ATOM}author"))
            out.append((RawItem(
                source="arxiv", external_id=arxiv_id, url=url,
                content_text=f"{title}\n\n{summary}", author=authors or None,
                published_at=_dt(e.findtext(f"{ATOM}published")), kind="paper"),
                github_repos(summary)))
        return out

    def _hf(self) -> list[tuple[RawItem, list[str]]]:
        resp = self.http.get(HF_URL)
        if resp.status_code != 200:
            raise RuntimeError(f"hf daily_papers returned {resp.status_code}")
        out = []
        for entry in resp.json():
            p: dict[str, Any] = entry.get("paper") or {}
            pid = p.get("id")
            if not pid:
                continue
            summary = _ws(p.get("summary"))
            out.append((RawItem(
                source="hf_papers", external_id=pid, url=f"https://huggingface.co/papers/{pid}",
                content_text=f"{_ws(p.get('title'))}\n\n{summary}",
                published_at=_dt(entry.get("publishedAt") or p.get("publishedAt")),
                kind="paper"),
                github_repos(summary, p.get("githubRepo"))))
        return out
