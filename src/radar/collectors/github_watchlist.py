import logging
from datetime import timedelta

from radar.collectors.base import Cursor, RawItem
from radar.collectors.github_client import GitHubClient, GitHubError

log = logging.getLogger(__name__)

SOURCE = "github_watchlist"
TEXT_EXTS = (".md", ".txt", ".json", ".yaml", ".yml", ".csv")
MAX_BYTES = 384 * 1024


def _is_text(path: str) -> bool:
    return path.lower().endswith(TEXT_EXTS)


def _added_lines(patch: str) -> str:
    return "\n".join(
        line[1:] for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


class GitHubWatchlist:
    name = "github_watchlist"
    interval = timedelta(minutes=10)

    def __init__(self, client: GitHubClient, conn):
        self.client = client
        self.conn = conn

    def poll(self, cursor: Cursor) -> tuple[list[RawItem], Cursor]:
        rows = self.conn.execute(
            "select full_name from watch_repos where status <> 'muted' order by full_name"
        ).fetchall()
        items: list[RawItem] = []
        new_cursor: Cursor = {}
        for row in rows:
            full = row["full_name"]
            state = cursor.get(full) or {}
            resp = self.client.get(
                f"/repos/{full}/commits", params={"per_page": 1}, etag=state.get("etag")
            )
            if resp.status == 304:
                new_cursor[full] = state
                continue
            if resp.status in (401, 403):
                raise GitHubError(f"GitHub {resp.status} on /repos/{full}/commits", resp.status)
            if resp.status != 200 or not resp.json:
                # 409 = empty repo, 404 = gone: skip silently, keep any old state
                if state:
                    new_cursor[full] = state
                continue
            head = resp.json[0]
            sha = head["sha"]
            author = (head.get("author") or {}).get("login")
            old = state.get("sha")
            if old == sha:
                pass
            elif old:
                items += self._diff_items(full, old, sha, author)
            else:
                items += self._first_sight_items(full, sha, author)
            new_cursor[full] = {"sha": sha, "etag": resp.etag}
        return items, new_cursor

    def _diff_items(self, full, old, new, author) -> list[RawItem]:
        resp = self.client.get(f"/repos/{full}/compare/{old}...{new}")
        if resp.status == 404:  # old sha gone (force-push): treat as first sight
            return self._first_sight_items(full, new, author)
        if resp.status != 200:
            raise GitHubError(f"GitHub {resp.status} on compare for {full}", resp.status)
        files = (resp.json or {}).get("files", [])
        if len(files) >= 300:
            log.warning("compare for %s returned 300 files; results may be truncated", full)
        out = []
        for f in files:
            name = f["filename"]
            if not _is_text(name) or not f.get("changes") or not f.get("patch"):
                continue
            text = _added_lines(f["patch"])
            if not text:
                continue
            out.append(RawItem(
                source=SOURCE,
                external_id=f"{full}@{new}:{name}",
                url=f.get("blob_url") or f"https://github.com/{full}/blob/{new}/{name}",
                content_text=text,
                author=author,
            ))
        return out

    def _first_sight_items(self, full, sha, author) -> list[RawItem]:
        resp = self.client.get(f"/repos/{full}/git/trees/{sha}", params={"recursive": 1})
        if resp.status != 200:
            raise GitHubError(f"GitHub {resp.status} on tree for {full}", resp.status)
        if (resp.json or {}).get("truncated"):
            log.warning("tree for %s@%s is truncated; some files will be missed", full, sha)
        out = []
        for node in (resp.json or {}).get("tree", []):
            path = node["path"]
            if node.get("type") != "blob" or not _is_text(path):
                continue
            if node.get("size", 0) > MAX_BYTES:
                continue
            body = self.client.get(f"/repos/{full}/contents/{path}", params={"ref": sha}, raw=True)
            if body.status == 404:  # deleted between tree and fetch
                continue
            if body.status != 200:
                raise GitHubError(f"GitHub {body.status} on contents {full}/{path}", body.status)
            if not body.json:
                continue
            out.append(RawItem(
                source=SOURCE,
                external_id=f"{full}@{sha}:{path}",
                url=f"https://github.com/{full}/blob/{sha}/{path}",
                content_text=body.json,
                author=author,
            ))
        return out
