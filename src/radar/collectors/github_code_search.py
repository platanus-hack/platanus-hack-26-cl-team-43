import time
from collections.abc import Callable
from datetime import timedelta

from radar.collectors.base import Cursor, RawItem
from radar.collectors.github_client import GitHubClient, GitHubError

SOURCE = "github_code"
EXTENSIONS = ("md", "txt")
MAX_MARKERS = 5  # 5 markers x 2 extensions = 10 search requests per run
SEARCH_PAUSE_S = 6
SEEN_CAP = 5000


class GitHubCodeSearch:
    name = "github_code_search"
    interval = timedelta(minutes=60)

    def __init__(
        self,
        client: GitHubClient,
        markers: list[str],
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.markers = markers[:MAX_MARKERS]
        self.sleep = sleep

    def poll(self, cursor: Cursor) -> tuple[list[RawItem], Cursor]:
        seen: list[str] = list(cursor.get("seen", []))
        seen_set = set(seen)
        items: list[RawItem] = []
        first = True
        for marker in self.markers:
            for ext in EXTENSIONS:
                if not first:
                    self.sleep(SEARCH_PAUSE_S)
                first = False
                resp = self.client.get(
                    "/search/code", params={"q": f'"{marker}" extension:{ext}', "per_page": 30}
                )
                if resp.status != 200:
                    raise GitHubError(f"GitHub {resp.status} on code search", resp.status)
                for hit in (resp.json or {}).get("items", []):
                    full = hit["repository"]["full_name"]
                    path = hit["path"]
                    ext_id = f"{full}:{path}@{hit['sha']}"
                    if ext_id in seen_set:
                        continue
                    body = self.client.get(f"/repos/{full}/contents/{path}", raw=True)
                    if body.status not in (200, 404):
                        raise GitHubError(
                            f"GitHub {body.status} on contents {full}/{path}", body.status
                        )
                    seen.append(ext_id)
                    seen_set.add(ext_id)
                    if body.status == 404 or not body.json:
                        continue
                    items.append(RawItem(
                        source=SOURCE,
                        external_id=ext_id,
                        url=hit.get("html_url") or f"https://github.com/{full}/blob/HEAD/{path}",
                        content_text=body.json,
                        author=(hit["repository"].get("owner") or {}).get("login"),
                    ))
        return items, {"seen": seen[-SEEN_CAP:]}
