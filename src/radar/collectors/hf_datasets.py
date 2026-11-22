import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from radar.collectors.base import Cursor, RawItem

log = logging.getLogger(__name__)

SOURCE = "hf_dataset"
VIEWER = "https://datasets-server.huggingface.co"
PROMPT_COLUMNS = ("prompt", "jailbreak", "text", "instruction", "query")
EXPAND = ["lastModified", "gated", "cardData", "sha"]
PAGE = 100
REQUEST_PAUSE_S = 0.5
BACKOFFS = (1, 2, 4)
FIRST_RUN_DAYS = 7


def pick_prompt_column(columns: list[str]) -> str | None:
    lowered = {c.lower(): c for c in reversed(columns)}
    for want in PROMPT_COLUMNS:
        if want in lowered:
            return lowered[want]
    return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _license(card_data: Any) -> str | None:
    if card_data is None:
        return None
    lic = card_data.get("license") if isinstance(card_data, dict) else getattr(
        card_data, "license", None)
    if isinstance(lic, (list, tuple)):
        lic = lic[0] if lic else None
    return lic or None


class _ViewerUnavailable(Exception):
    pass


class HFDatasets:
    name = "hf_datasets"
    interval = timedelta(minutes=60)

    def __init__(
        self,
        api: Any,
        http: httpx.Client,
        keywords: list[str],
        max_rows: int = 2000,
        hf_token: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.api = api
        self.http = http
        self.keywords = keywords
        self.max_rows = max_rows
        self.headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
        self.sleep = sleep
        self.now = now
        self._requested = False

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(len(BACKOFFS) + 1):
            if self._requested:
                self.sleep(REQUEST_PAUSE_S)
            self._requested = True
            resp = self.http.get(f"{VIEWER}{path}", params=params, headers=self.headers)
            if resp.status_code == 429:
                if attempt < len(BACKOFFS):
                    self.sleep(BACKOFFS[attempt])
                    continue
                resp.raise_for_status()
            if 400 <= resp.status_code < 500:
                raise _ViewerUnavailable(f"{resp.status_code} on {path}")
            resp.raise_for_status()
            return resp.json()
        raise AssertionError("unreachable")

    def poll(self, cursor: Cursor) -> tuple[list[RawItem], Cursor]:
        if cursor.get("since"):
            since = _aware(datetime.fromisoformat(cursor["since"]))
        else:
            since = self.now() - timedelta(days=FIRST_RUN_DAYS)
        newest = since
        seen: set[str] = set()
        items: list[RawItem] = []
        for kw in self.keywords:
            results = self.api.list_datasets(
                search=kw, sort="last_modified", expand=EXPAND)  # always descending
            for ds in results:
                modified = _aware(ds.last_modified)
                if modified <= since:
                    break
                newest = max(newest, modified)
                if ds.id in seen:
                    continue
                seen.add(ds.id)
                items.extend(self._dataset_items(ds))
        return items, {"since": newest.isoformat()}

    def _dataset_items(self, ds: Any) -> list[RawItem]:
        lic = _license(ds.card_data)
        url = f"https://huggingface.co/datasets/{ds.id}"
        if ds.gated:
            return [RawItem(source=SOURCE, external_id=f"{ds.id}@{ds.sha}:gated", url=url,
                            content_text="", license=lic, kind="text")]
        try:
            return list(self._rows(ds, url, lic))
        except _ViewerUnavailable as e:
            log.warning("hf viewer unavailable for %s: %s", ds.id, e)
            return []

    def _rows(self, ds: Any, url: str, lic: str | None) -> Iterator[RawItem]:
        splits = self._get("/splits", {"dataset": ds.id}).get("splits", [])
        remaining = self.max_rows
        column: str | None = None
        for sp in splits:
            offset = 0
            while remaining > 0:
                data = self._get("/rows", {
                    "dataset": ds.id, "config": sp["config"], "split": sp["split"],
                    "offset": offset, "length": PAGE})
                rows = data.get("rows", [])
                if column is None:
                    names = [f["name"] for f in data.get("features", [])]
                    column = pick_prompt_column(names)
                    if column is None:
                        log.info("hf dataset %s has no prompt column, skipping", ds.id)
                        return
                for r in rows[:remaining]:
                    value = r.get("row", {}).get(column)
                    if isinstance(value, str) and value:
                        yield RawItem(
                            source=SOURCE,
                            external_id=f"{ds.id}@{ds.sha}:{sp['split']}:{r['row_idx']}",
                            url=url, content_text=value, license=lic)
                taken = min(len(rows), remaining)
                remaining -= taken
                offset += len(rows)
                if not rows or offset >= data.get("num_rows_total", 0):
                    break
