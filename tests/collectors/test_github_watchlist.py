import httpx
import pytest
import respx

from radar.collectors.github_client import GitHubClient
from radar.collectors.github_watchlist import GitHubWatchlist

API = "https://api.github.com"


def _make(conn, repos=(("a/b", "seed"),)):
    for full, status in repos:
        conn.execute("insert into watch_repos (full_name, status) values (%s, %s)", (full, status))
    return GitHubWatchlist(GitHubClient("tok"), conn)


def _commit(sha, login="alice"):
    return [{"sha": sha, "author": {"login": login}}]


@respx.mock
def test_304_emits_nothing(conn):
    wl = _make(conn)
    route = respx.get(f"{API}/repos/a/b/commits").respond(304)
    items, cursor = wl.poll({"a/b": {"sha": "s1", "etag": '"e1"'}})
    assert items == []
    assert cursor == {"a/b": {"sha": "s1", "etag": '"e1"'}}
    assert route.calls[0].request.headers["if-none-match"] == '"e1"'
    assert route.calls[0].request.url.params["per_page"] == "1"


@respx.mock
def test_new_commit_emits_only_added_lines(conn):
    wl = _make(conn)
    respx.get(f"{API}/repos/a/b/commits").respond(200, json=_commit("s2"), headers={"etag": '"e2"'})
    respx.get(f"{API}/repos/a/b/compare/s1...s2").respond(200, json={"files": [
        {"filename": "p.md", "changes": 2, "blob_url": "https://github.com/a/b/blob/s2/p.md",
         "patch": "@@ -1 +1,2 @@\n old\n+new jailbreak line"},
    ]})
    items, cursor = wl.poll({"a/b": {"sha": "s1", "etag": '"e1"'}})
    assert len(items) == 1
    it = items[0]
    assert it.source == "github_watchlist"
    assert it.content_text == "new jailbreak line"
    assert it.external_id == "a/b@s2:p.md"
    assert it.url == "https://github.com/a/b/blob/s2/p.md"
    assert it.author == "alice"
    assert cursor == {"a/b": {"sha": "s2", "etag": '"e2"'}}


@respx.mock
def test_ignores_non_text_files(conn):
    wl = _make(conn)
    respx.get(f"{API}/repos/a/b/commits").respond(200, json=_commit("s2"))
    respx.get(f"{API}/repos/a/b/compare/s1...s2").respond(200, json={"files": [
        {"filename": "x.png", "changes": 1, "blob_url": "u", "patch": "@@\n+bin"},
        {"filename": "x.py", "changes": 1, "blob_url": "u", "patch": "@@\n+code"},
        {"filename": "empty.md", "changes": 0, "blob_url": "u"},
        {"filename": "del.md", "changes": 1, "blob_url": "u", "patch": "@@\n-gone"},
    ]})
    items, cursor = wl.poll({"a/b": {"sha": "s1", "etag": None}})
    assert items == []
    assert cursor["a/b"]["sha"] == "s2"


@respx.mock
def test_first_sight_emits_full_files_under_384kb(conn):
    wl = _make(conn)
    respx.get(f"{API}/repos/a/b/commits").respond(200, json=_commit("h1"))
    respx.get(f"{API}/repos/a/b/git/trees/h1").respond(200, json={"tree": [
        {"path": "a.md", "type": "blob", "size": 10},
        {"path": "dir/b.txt", "type": "blob", "size": 384 * 1024},
        {"path": "big.json", "type": "blob", "size": 384 * 1024 + 1},
        {"path": "c.py", "type": "blob", "size": 5},
        {"path": "dir", "type": "tree"},
    ]})
    ra = respx.get(f"{API}/repos/a/b/contents/a.md").respond(200, text="AAA")
    respx.get(f"{API}/repos/a/b/contents/dir/b.txt").respond(200, text="BBB")
    items, cursor = wl.poll({})
    assert {i.external_id: i.content_text for i in items} == {
        "a/b@h1:a.md": "AAA", "a/b@h1:dir/b.txt": "BBB"}
    assert ra.calls[0].request.url.params["ref"] == "h1"
    assert ra.calls[0].request.headers["accept"] == "application/vnd.github.raw+json"
    assert items[0].url == "https://github.com/a/b/blob/h1/a.md"
    assert cursor["a/b"]["sha"] == "h1"


@respx.mock
def test_muted_repo_skipped(conn):
    wl = _make(conn, [("a/b", "muted")])
    route = respx.get(f"{API}/repos/a/b/commits")
    items, cursor = wl.poll({})
    assert items == [] and cursor == {}
    assert not route.called


@respx.mock
def test_empty_repo_409_skipped(conn):
    wl = _make(conn)
    respx.get(f"{API}/repos/a/b/commits").respond(409, json={"message": "Git Repository is empty."})
    items, cursor = wl.poll({})
    assert items == [] and cursor == {}


@respx.mock
def test_client_follows_redirects_for_renamed_repo(conn):
    respx.get(f"{API}/repos/old/name/commits").respond(
        301, headers={"location": f"{API}/repositories/1/commits"})
    respx.get(f"{API}/repositories/1/commits").respond(200, json=[{"sha": "z"}])
    resp = GitHubClient("tok").get("/repos/old/name/commits")
    assert resp.status == 200 and resp.json[0]["sha"] == "z"


@respx.mock
def test_contents_500_skips_repo_and_runner_keeps_its_old_cursor(conn, caplog):
    from radar.collectors.runner import load_cursor, run_collector, save_cursor

    wl = _make(conn)
    save_cursor(conn, "github_watchlist", {})
    conn.commit()
    respx.get(f"{API}/repos/a/b/commits").respond(200, json=_commit("h1"))
    respx.get(f"{API}/repos/a/b/git/trees/h1").respond(200, json={"tree": [
        {"path": "a.md", "type": "blob", "size": 1}, {"path": "b.md", "type": "blob", "size": 1}]})
    respx.get(f"{API}/repos/a/b/contents/a.md").respond(200, text="AAA")
    respx.get(f"{API}/repos/a/b/contents/b.md").respond(500)
    with caplog.at_level("WARNING"):
        assert run_collector(conn, wl) == 0
    assert load_cursor(conn, "github_watchlist") == {}
    assert conn.execute("select count(*) as n from raw_items").fetchone()["n"] == 0
    assert any("a/b" in r.message and r.levelname == "WARNING" for r in caplog.records)


@respx.mock
def test_compare_404_falls_back_to_first_sight(conn):
    wl = _make(conn)
    respx.get(f"{API}/repos/a/b/commits").respond(200, json=_commit("s2"))
    respx.get(f"{API}/repos/a/b/compare/s1...s2").respond(404)
    respx.get(f"{API}/repos/a/b/git/trees/s2").respond(200, json={"tree": [
        {"path": "a.md", "type": "blob", "size": 1}]})
    respx.get(f"{API}/repos/a/b/contents/a.md").respond(200, text="AAA")
    items, cursor = wl.poll({"a/b": {"sha": "s1", "etag": None}})
    assert [i.external_id for i in items] == ["a/b@s2:a.md"]
    assert cursor["a/b"]["sha"] == "s2"


@respx.mock
def test_commits_403_raises(conn):
    import pytest

    from radar.collectors.github_client import GitHubError

    wl = _make(conn)
    respx.get(f"{API}/repos/a/b/commits").respond(403, json={"message": "Bad credentials"})
    with pytest.raises(GitHubError):
        wl.poll({})


@respx.mock
def test_truncated_tree_logs_warning(conn, caplog):
    wl = _make(conn)
    respx.get(f"{API}/repos/a/b/commits").respond(200, json=_commit("h1"))
    respx.get(f"{API}/repos/a/b/git/trees/h1").respond(200, json={"truncated": True, "tree": [
        {"path": "a.md", "type": "blob", "size": 1}]})
    respx.get(f"{API}/repos/a/b/contents/a.md").respond(200, text="AAA")
    with caplog.at_level("WARNING"):
        items, _ = wl.poll({})
    assert len(items) == 1
    assert any("a/b" in r.message and r.levelname == "WARNING" for r in caplog.records)


@respx.mock
@pytest.mark.parametrize("status", ["auto", "from_paper"])
def test_auto_repo_first_sight_records_sha_and_emits_nothing(conn, status):
    wl = _make(conn, [("a/b", status)])
    respx.get(f"{API}/repos/a/b/commits").respond(200, json=_commit("h1"),
                                                   headers={"etag": '"e1"'})
    tree = respx.get(f"{API}/repos/a/b/git/trees/h1")
    items, cursor = wl.poll({})
    assert items == []
    assert cursor == {"a/b": {"sha": "h1", "etag": '"e1"'}}
    assert not tree.called


@respx.mock
def test_seed_repo_first_sight_emits_files(conn):
    wl = _make(conn, [("a/b", "seed")])
    respx.get(f"{API}/repos/a/b/commits").respond(200, json=_commit("h1"))
    respx.get(f"{API}/repos/a/b/git/trees/h1").respond(200, json={"tree": [
        {"path": "p.mkd", "type": "blob", "size": 1},
        {"path": "q.markdown", "type": "blob", "size": 1}]})
    respx.get(f"{API}/repos/a/b/contents/p.mkd").respond(200, text="PPP")
    respx.get(f"{API}/repos/a/b/contents/q.markdown").respond(200, text="QQQ")
    items, cursor = wl.poll({})
    assert sorted(i.content_text for i in items) == ["PPP", "QQQ"]
    assert cursor["a/b"]["sha"] == "h1"


@respx.mock
def test_rate_limit_midway_keeps_progress(conn):
    wl = _make(conn, [("a/a", "seed"), ("a/b", "seed"), ("a/c", "seed")])
    respx.get(f"{API}/repos/a/a/commits").respond(200, json=_commit("s2"))
    respx.get(f"{API}/repos/a/a/compare/s1...s2").respond(200, json={"files": [
        {"filename": "p.md", "changes": 1, "patch": "@@\n+new line"}]})
    respx.get(f"{API}/repos/a/b/commits").respond(
        403, headers={"x-ratelimit-remaining": "0"}, json={"message": "rate limit"})
    c = respx.get(f"{API}/repos/a/c/commits")
    old = {"a/a": {"sha": "s1", "etag": None}, "a/b": {"sha": "b1", "etag": '"eb"'},
           "a/c": {"sha": "c1", "etag": '"ec"'}}
    items, cursor = wl.poll(old)
    assert [i.content_text for i in items] == ["new line"]
    assert cursor == {"a/a": {"sha": "s2", "etag": None}, "a/b": old["a/b"], "a/c": old["a/c"]}
    assert not c.called


@respx.mock
def test_500_on_one_repo_does_not_block_others(conn, caplog):
    wl = _make(conn, [("a/a", "seed"), ("a/b", "seed")])
    respx.get(f"{API}/repos/a/a/commits").respond(500)
    respx.get(f"{API}/repos/a/b/commits").respond(200, json=_commit("s2"))
    respx.get(f"{API}/repos/a/b/compare/s1...s2").respond(200, json={"files": [
        {"filename": "p.md", "changes": 1, "patch": "@@\n+new line"}]})
    old = {"a/a": {"sha": "x1", "etag": None}, "a/b": {"sha": "s1", "etag": None}}
    with caplog.at_level("WARNING"):
        items, cursor = wl.poll(old)
    assert [i.content_text for i in items] == ["new line"]
    assert cursor == {"a/a": old["a/a"], "a/b": {"sha": "s2", "etag": None}}
    assert any("a/a" in r.message and r.levelname == "WARNING" for r in caplog.records)
