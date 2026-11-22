from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = "https://api.github.com"


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GitHubRateLimited(GitHubError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message, status=429)
        self.retry_after = retry_after


@dataclass(frozen=True)
class GitHubResponse:
    status: int
    json: Any
    etag: str | None
    headers: Mapping[str, str]


class GitHubClient:
    def __init__(self, token: str, http: httpx.Client | None = None):
        self._token = token
        self._http = http or httpx.Client(base_url=BASE_URL, timeout=30)

    def get(
        self,
        path: str,
        params: dict | None = None,
        etag: str | None = None,
        raw: bool = False,
    ) -> GitHubResponse:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if etag:
            headers["If-None-Match"] = etag
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        resp = self._http.get(url, params=params, headers=headers)
        if resp.status_code in (403, 429) and (
            resp.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in resp.headers
        ):
            retry = resp.headers.get("retry-after")
            raise GitHubRateLimited(
                f"GitHub rate limited on {path}", float(retry) if retry else None
            )
        if resp.status_code >= 500:
            raise GitHubError(f"GitHub {resp.status_code} on {path}", resp.status_code)
        body: Any = None
        if resp.status_code != 304:
            if raw:
                body = resp.text
            else:
                try:
                    body = resp.json()
                except ValueError:
                    body = None
        return GitHubResponse(resp.status_code, body, resp.headers.get("etag"), resp.headers)
