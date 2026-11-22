from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from radar.collectors.base import Cursor, RawItem
from radar.collectors.github_client import GitHubClient


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GitHubDiscovery:
    name = "github_discovery"
    interval = timedelta(minutes=30)

    def __init__(
        self,
        client: GitHubClient,
        conn,
        queries: list[str],
        now: Callable[[], datetime] = _utcnow,
    ):
        self.client = client
        self.conn = conn
        self.queries = queries
        self.now = now

    def poll(self, cursor: Cursor) -> tuple[list[RawItem], Cursor]:
        started = self.now()
        since = cursor.get("since") or (started - timedelta(days=7)).isoformat()
        repos: dict[str, dict] = {}
        for query in self.queries:
            resp = self.client.get(
                "/search/repositories",
                params={"q": f"{query} pushed:>{since}", "sort": "updated", "per_page": 50},
            )
            for repo in (resp.json or {}).get("items", []):
                repos.setdefault(repo["full_name"], repo)

        items: list[RawItem] = []
        for full_name, repo in repos.items():
            self.conn.execute(
                "insert into watch_repos (full_name, status) values (%s, 'auto') "
                "on conflict (full_name) do nothing",
                (full_name,),
            )
            readme = self.client.get(f"/repos/{full_name}/readme", raw=True)
            if readme.status == 404 or not readme.json:
                continue
            pushed = repo.get("pushed_at")
            items.append(
                RawItem(
                    source="github_readme",
                    external_id=full_name,
                    url=repo["html_url"],
                    content_text=readme.json,
                    author=(repo.get("owner") or {}).get("login"),
                    license=((repo.get("license") or {}).get("spdx_id")),
                    published_at=datetime.fromisoformat(pushed.replace("Z", "+00:00"))
                    if pushed
                    else None,
                )
            )
        return items, {"since": started.isoformat()}
