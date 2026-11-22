from datetime import timedelta

import httpx
import pytest
import respx

from radar.collectors.papers import Papers

ARXIV = "http://export.arxiv.org/api/query"
HF = "https://huggingface.co/api/daily_papers"

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry>
  <id>http://arxiv.org/abs/2401.01234v2</id>
  <published>2024-01-02T10:00:00Z</published>
  <title>A  Jailbreak
   Paper</title>
  <summary>We release code at https://github.com/Foo-Bar/jb.repo.git and also
  see github.com/baz/qux. Thanks.</summary>
  <author><name>Ann</name></author><author><name>Bob</name></author>
 </entry>
</feed>"""

HF_JSON = [
    {"paper": {"id": "2402.00001", "title": "HF Paper", "summary": "no links",
               "githubRepo": "https://github.com/hf/Repo"},
     "publishedAt": "2024-02-01T00:00:00.000Z"},
    {"paper": {"id": "2401.09999", "title": "other", "summary": "x"}},
]


def _make(conn):
    return Papers(conn, httpx.Client())


@respx.mock
def test_papers_items_are_kind_paper(conn):
    arx = respx.get(ARXIV).respond(200, text=ATOM)
    respx.get(HF).respond(200, json=HF_JSON)
    p = _make(conn)
    items, cur = p.poll({})
    assert p.name == "papers" and p.interval == timedelta(hours=6)
    assert all(i.kind == "paper" for i in items)
    by = {i.external_id: i for i in items}
    a = by["2401.01234"]
    assert a.source == "arxiv" and a.url == "http://arxiv.org/abs/2401.01234"
    assert a.content_text.startswith("A Jailbreak Paper")
    assert a.author == "Ann, Bob" and a.published_at.year == 2024
    assert by["2402.00001"].source == "hf_papers"
    assert by["2402.00001"].url == "https://huggingface.co/papers/2402.00001"
    assert set(cur["seen"]) == {"arxiv:2401.01234", "hf_papers:2402.00001", "hf_papers:2401.09999"}
    q = arx.calls[0].request.url.params
    assert q["search_query"] == 'abs:jailbreak AND (abs:LLM OR abs:"language model")'
    assert q["sortBy"] == "submittedDate" and q["sortOrder"] == "descending"
    assert q["max_results"] == "50"
    # second poll: seen items are skipped
    items2, _ = p.poll(cur)
    assert items2 == []


@respx.mock
def test_papers_adds_github_links_to_watchlist(conn):
    respx.get(ARXIV).respond(200, text=ATOM)
    respx.get(HF).respond(200, json=HF_JSON)
    conn.execute("insert into watch_repos (full_name, status) values ('baz/qux', 'seed')")
    _make(conn).poll({})
    rows = {r["full_name"]: r["status"]
            for r in conn.execute("select full_name, status from watch_repos")}
    assert rows == {"baz/qux": "seed", "Foo-Bar/jb.repo": "from_paper", "hf/Repo": "from_paper"}


@respx.mock
@pytest.mark.parametrize("bad", ["arxiv", "hf"])
def test_non_200_raises(conn, bad):
    respx.get(ARXIV).respond(500 if bad == "arxiv" else 200, text=ATOM)
    respx.get(HF).respond(503 if bad == "hf" else 200, json=HF_JSON)
    with pytest.raises(Exception):
        _make(conn).poll({})


@respx.mock
def test_seen_capped(conn):
    respx.get(ARXIV).respond(200, text=ATOM)
    respx.get(HF).respond(200, json=[])
    _, cur = _make(conn).poll({"seen": [f"x{i}" for i in range(2500)]})
    assert len(cur["seen"]) == 2000 and cur["seen"][-1] == "arxiv:2401.01234"
