from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx

from radar.collectors.hf_datasets import HFDatasets, pick_prompt_column

VIEWER = "https://datasets-server.huggingface.co"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


class FakeApi:
    def __init__(self, by_kw):
        self.by_kw = by_kw
        self.calls = []

    def list_datasets(self, **kw):
        self.calls.append(kw)
        return iter(self.by_kw.get(kw["search"], []))


def _ds(id="o/d", days_ago=1, gated=False, license="mit", sha="abc"):
    return SimpleNamespace(
        id=id, last_modified=NOW - timedelta(days=days_ago), gated=gated, sha=sha,
        card_data=SimpleNamespace(license=license) if license is not None else None)


def _make(api, kws=("jailbreak",), max_rows=2000, token=None, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    c = HFDatasets(api, httpx.Client(), list(kws), max_rows=max_rows, hf_token=token,
                   sleep=sleeps.append, now=lambda: NOW)
    return c, sleeps


def _splits(id="o/d", pairs=(("default", "train"),)):
    respx.get(f"{VIEWER}/splits", params={"dataset": id}).respond(
        200, json={"splits": [{"dataset": id, "config": c, "split": s} for c, s in pairs]})


def _rows(cols, values, total=None, **params):
    return httpx.Response(200, json={
        "features": [{"name": c} for c in cols],
        "rows": [{"row_idx": i, "row": v} for i, v in enumerate(values)],
        "num_rows_total": total if total is not None else len(values)})


def test_pick_prompt_column():
    assert pick_prompt_column(["id", "Prompt", "label"]) == "Prompt"
    assert pick_prompt_column(["a", "b"]) is None


def test_stops_at_cursor_date():
    api = FakeApi({"jailbreak": [_ds("o/new", 1, gated=True), _ds("o/old", 5, gated=True)]})
    c, _ = _make(api)
    since = (NOW - timedelta(days=3)).isoformat()
    items, cursor = c.poll({"since": since})
    assert [i.external_id for i in items] == ["o/new@abc:gated"]
    assert cursor == {"since": (NOW - timedelta(days=1)).isoformat()}
    kw = api.calls[0]
    assert kw["sort"] == "last_modified" and "direction" not in kw  # removed in hub 1.x
    assert kw["expand"] == ["lastModified", "gated", "cardData", "sha"]


def test_first_run_uses_seven_days_and_keeps_since_when_nothing_new():
    api = FakeApi({"jailbreak": [_ds("o/old", 9, gated=True)]})
    c, _ = _make(api)
    items, cursor = c.poll({})
    assert items == []
    assert cursor == {"since": (NOW - timedelta(days=7)).isoformat()}


def test_gated_dataset_not_read():
    api = FakeApi({"jailbreak": [_ds(gated=True, license="cc-by")]})
    c, _ = _make(api)
    with respx.mock:  # no routes: any request would raise
        items, _ = c.poll({})
    assert len(items) == 1
    it = items[0]
    assert (it.source, it.kind, it.content_text) == ("hf_dataset", "text", "")
    assert it.external_id == "o/d@abc:gated"
    assert it.url == "https://huggingface.co/datasets/o/d"
    assert it.license == "cc-by"


@respx.mock
def test_rows_become_raw_items_with_license():
    api = FakeApi({"jailbreak": [_ds(license=["apache-2.0", "mit"])],
                   "red-team": [_ds()]})  # deduped across keywords
    c, _ = _make(api, kws=("jailbreak", "red-team"), token="tok")
    _splits(pairs=(("default", "train"), ("default", "test")))
    route = respx.get(f"{VIEWER}/rows").mock(side_effect=lambda req: (
        _rows(["id", "Prompt"], [{"id": 1, "Prompt": "do bad"}, {"id": 2, "Prompt": ""},
                                 {"id": 3, "Prompt": None}, {"id": 4, "Prompt": "more"}])
        if req.url.params["split"] == "train"
        else _rows(["id", "Prompt"], [{"id": 5, "Prompt": "t"}])))
    items, _ = c.poll({})
    assert [i.external_id for i in items] == [
        "o/d@abc:train:0", "o/d@abc:train:3", "o/d@abc:test:0"]
    assert items[0].content_text == "do bad"
    assert items[0].source == "hf_dataset"
    assert items[0].license == "apache-2.0"
    assert items[0].url == "https://huggingface.co/datasets/o/d"
    assert route.calls[0].request.headers["authorization"] == "Bearer tok"
    assert route.calls[0].request.url.params["length"] == "100"
    assert route.call_count == 2


@respx.mock
def test_max_rows_and_paging():
    api = FakeApi({"jailbreak": [_ds()]})
    c, _ = _make(api, max_rows=150)
    _splits()
    route = respx.get(f"{VIEWER}/rows").mock(side_effect=lambda req: _rows(
        ["prompt"], [{"prompt": f"p{i}"} for i in range(100)], total=1000))
    items, _ = c.poll({})
    assert len(items) == 150
    assert route.call_count == 2


@respx.mock
def test_no_prompt_column_skips_dataset():
    api = FakeApi({"jailbreak": [_ds()]})
    c, _ = _make(api)
    _splits()
    respx.get(f"{VIEWER}/rows").mock(return_value=_rows(["a"], [{"a": "x"}]))
    items, _ = c.poll({})
    assert items == []


@respx.mock
def test_viewer_4xx_skips_dataset():
    api = FakeApi({"jailbreak": [_ds("o/a"), _ds("o/b")]})
    c, _ = _make(api)
    respx.get(f"{VIEWER}/splits", params={"dataset": "o/a"}).respond(404)
    _splits("o/b")
    respx.get(f"{VIEWER}/rows", params={"dataset": "o/b"}).mock(
        return_value=_rows(["text"], [{"text": "x"}]))
    items, _ = c.poll({})
    assert [i.external_id for i in items] == ["o/b@abc:train:0"]


@respx.mock
def test_429_backs_off_then_succeeds():
    api = FakeApi({"jailbreak": [_ds()]})
    c, sleeps = _make(api)
    respx.get(f"{VIEWER}/splits").mock(side_effect=[
        httpx.Response(429), httpx.Response(429),
        httpx.Response(200, json={"splits": [{"config": "default", "split": "train"}]})])
    respx.get(f"{VIEWER}/rows").mock(return_value=_rows(["text"], [{"text": "x"}]))
    items, _ = c.poll({})
    assert len(items) == 1
    assert [s for s in sleeps if s >= 1] == [1, 2]


@respx.mock
def test_429_exhausted_raises_and_5xx_raises():
    api = FakeApi({"jailbreak": [_ds()]})
    c, sleeps = _make(api)
    respx.get(f"{VIEWER}/splits").respond(429)
    with pytest.raises(httpx.HTTPStatusError):
        c.poll({})
    assert [s for s in sleeps if s >= 1] == [1, 2, 4]
    respx.get(f"{VIEWER}/splits").respond(503)
    with pytest.raises(httpx.HTTPStatusError):
        c.poll({})
