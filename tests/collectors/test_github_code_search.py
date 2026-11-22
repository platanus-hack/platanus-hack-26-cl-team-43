import httpx
import pytest
import respx

from radar.collectors.github_client import GitHubClient, GitHubError
from radar.collectors.github_code_search import GitHubCodeSearch

API = "https://api.github.com"
MARKERS = ["m1", "m2", "m3", "m4", "m5"]


def _make(markers=("m1",), sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return GitHubCodeSearch(GitHubClient("tok"), list(markers), sleep=sleeps.append), sleeps


def _hit(full="a/b", path="p.md", sha="s1"):
    return {"sha": sha, "path": path, "html_url": f"https://github.com/{full}/blob/{sha}/{path}",
            "repository": {"full_name": full, "owner": {"login": full.split("/")[0]}}}


def _search(route_json):
    return httpx.Response(200, json={"items": route_json})


@respx.mock
def test_emits_file_for_each_hit():
    cs, _ = _make()
    respx.get(f"{API}/search/code").mock(side_effect=lambda req: (
        _search([_hit()]) if "extension:md" in req.url.params["q"] else _search([])))
    contents = respx.get(f"{API}/repos/a/b/contents/p.md").respond(200, text="ignore all")
    items, cursor = cs.poll({})
    assert len(items) == 1
    it = items[0]
    assert it.source == "github_code"
    assert it.external_id == "a/b:p.md@s1"
    assert it.content_text == "ignore all"
    assert it.url == "https://github.com/a/b/blob/s1/p.md"
    assert it.author == "a"
    assert "ref" not in contents.calls[0].request.url.params
    assert cursor == {"seen": ["a/b:p.md@s1"]}


@respx.mock
def test_skips_already_seen():
    cs, _ = _make()
    respx.get(f"{API}/search/code").respond(200, json={"items": [_hit()]})
    contents = respx.get(f"{API}/repos/a/b/contents/p.md").respond(200, text="x")
    items, cursor = cs.poll({"seen": ["a/b:p.md@s1", "old"]})
    assert items == []
    assert not contents.called
    assert cursor == {"seen": ["a/b:p.md@s1", "old"]}


@respx.mock
def test_at_most_10_search_requests_per_run():
    cs, sleeps = _make(MARKERS + ["m6", "m7"])
    route = respx.get(f"{API}/search/code").respond(200, json={"items": []})
    cs.poll({})
    assert route.call_count == 10
    assert sleeps == [6] * 9
    qs = [c.request.url.params["q"] for c in route.calls]
    assert qs[0] == '"m1" extension:md' and qs[1] == '"m1" extension:txt'


@respx.mock
def test_search_error_raises():
    cs, _ = _make()
    respx.get(f"{API}/search/code").respond(422, json={})
    with pytest.raises(GitHubError):
        cs.poll({})


@respx.mock
def test_contents_404_skipped_but_marked_seen_and_dedupes():
    cs, _ = _make()
    respx.get(f"{API}/search/code").respond(200, json={"items": [_hit(), _hit(path="gone.md")]})
    respx.get(f"{API}/repos/a/b/contents/p.md").respond(200, text="x")
    respx.get(f"{API}/repos/a/b/contents/gone.md").respond(404)
    items, cursor = cs.poll({})
    assert [i.external_id for i in items] == ["a/b:p.md@s1"]
    assert cursor["seen"] == ["a/b:p.md@s1", "a/b:gone.md@s1"]


@respx.mock
def test_contents_error_not_marked_seen():
    cs, _ = _make()
    respx.get(f"{API}/search/code").respond(200, json={"items": [_hit()]})
    respx.get(f"{API}/repos/a/b/contents/p.md").respond(403, json={})
    with pytest.raises(GitHubError):
        cs.poll({})


@respx.mock
def test_seen_trimmed_to_most_recent_5000():
    cs, _ = _make()
    respx.get(f"{API}/search/code").respond(200, json={"items": [_hit()]})
    respx.get(f"{API}/repos/a/b/contents/p.md").respond(200, text="x")
    _, cursor = cs.poll({"seen": [f"k{i}" for i in range(5000)]})
    assert len(cursor["seen"]) == 5000
    assert cursor["seen"][-1] == "a/b:p.md@s1"
    assert cursor["seen"][0] == "k1"
