from datetime import datetime, timezone

import httpx
import pytest
import respx

from radar.collectors.github_client import GitHubClient, GitHubRateLimited
from radar.collectors.github_discovery import GitHubDiscovery

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def _search_payload(full="a/b"):
    return {"items": [{"full_name": full, "html_url": f"https://github.com/{full}",
                       "owner": {"login": full.split("/")[0]}, "pushed_at": "2026-11-20T00:00:00Z"}]}


def _make(conn, queries=("jailbreak llm",)):
    return GitHubDiscovery(GitHubClient("tok"), conn, list(queries), now=lambda: NOW)


@respx.mock
def test_discovery_adds_repos_to_watchlist_as_auto(conn):
    route = respx.get("https://api.github.com/search/repositories").respond(
        200, json=_search_payload())
    respx.get("https://api.github.com/repos/a/b/readme").respond(200, text="# hi")
    items, cursor = _make(conn).poll({})
    row = conn.execute("select status from watch_repos where full_name='a/b'").fetchone()
    assert row["status"] == "auto"
    req = route.calls[0].request
    assert req.url.params["q"] == "jailbreak llm pushed:>2026-11-14T12:00:00+00:00"
    assert req.url.params["sort"] == "updated"
    assert req.url.params["per_page"] == "50"
    assert req.headers["authorization"] == "Bearer tok"
    assert cursor == {"since": NOW.isoformat()}


@respx.mock
def test_discovery_does_not_downgrade_muted_repo(conn):
    conn.execute("insert into watch_repos (full_name, status) values ('a/b','muted')")
    respx.get("https://api.github.com/search/repositories").respond(200, json=_search_payload())
    respx.get("https://api.github.com/repos/a/b/readme").respond(200, text="x")
    _make(conn).poll({"since": "2026-11-01T00:00:00+00:00"})
    row = conn.execute("select status from watch_repos where full_name='a/b'").fetchone()
    assert row["status"] == "muted"


@respx.mock
def test_discovery_emits_readme_raw_item(conn):
    respx.get("https://api.github.com/search/repositories").respond(200, json=_search_payload())
    readme = respx.get("https://api.github.com/repos/a/b/readme").respond(200, text="# jailbreak")
    items, _ = _make(conn).poll({})
    assert len(items) == 1
    it = items[0]
    assert it.source == "github_readme"
    assert it.external_id == "a/b"
    assert it.url == "https://github.com/a/b"
    assert it.content_text == "# jailbreak"
    assert readme.calls[0].request.headers["accept"] == "application/vnd.github.raw+json"


@respx.mock
def test_readme_404_skips_item_but_adds_repo(conn):
    respx.get("https://api.github.com/search/repositories").respond(200, json=_search_payload())
    respx.get("https://api.github.com/repos/a/b/readme").respond(404, json={})
    items, _ = _make(conn).poll({})
    assert items == []
    assert conn.execute("select count(*) n from watch_repos").fetchone()["n"] == 1


@respx.mock
def test_rate_limit_raises():
    respx.get("https://api.github.com/x").respond(
        403, headers={"x-ratelimit-remaining": "0"}, json={})
    with pytest.raises(GitHubRateLimited):
        GitHubClient("tok").get("/x")


@respx.mock
def test_etag_not_modified():
    route = respx.get("https://api.github.com/x").respond(304, headers={"etag": 'W/"1"'})
    r = GitHubClient("tok").get("/x", etag='W/"1"')
    assert r.status == 304
    assert route.calls[0].request.headers["if-none-match"] == 'W/"1"'
