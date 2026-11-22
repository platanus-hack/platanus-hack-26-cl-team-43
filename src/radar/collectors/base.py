import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

Cursor = dict[str, Any]


@dataclass(frozen=True)
class RawItem:
    source: str
    external_id: str
    url: str
    content_text: str
    author: str | None = None
    license: str | None = None
    published_at: datetime | None = None
    kind: Literal["text", "paper"] = "text"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content_text.encode()).hexdigest()


class Collector(Protocol):
    name: str
    interval: timedelta

    def poll(self, cursor: Cursor) -> tuple[list[RawItem], Cursor]: ...
