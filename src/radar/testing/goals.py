from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml


@dataclass(frozen=True)
class Goal:
    id: str
    kind: Literal["canary", "policy"]
    text: str
    rubric: str | None


@dataclass(frozen=True)
class ModelSpec:
    id: str
    openrouter_id: str
    tier: Literal[1, 2]
    enabled: bool


def load_goals(path: Path) -> list[Goal]:
    data = yaml.safe_load(Path(path).read_text())
    return [
        Goal(id=g["id"], kind=g["kind"], text=g["text"], rubric=g.get("rubric"))
        for g in data["goals"]
    ]


def load_models(path: Path) -> list[ModelSpec]:
    data = yaml.safe_load(Path(path).read_text())
    return [
        ModelSpec(
            id=m["id"],
            openrouter_id=m["openrouter_id"],
            tier=m["tier"],
            enabled=m.get("enabled", True),
        )
        for m in data["models"]
    ]


def sync_config(conn, goals: list[Goal], models: list[ModelSpec]) -> None:
    """Upsert goals and models; the caller commits.

    yaml is the source of truth, except a model already disabled in the DB
    stays disabled (it is disabled at runtime when removed upstream) unless its
    yaml openrouter_id changed, which re-enables it.
    """
    for g in goals:
        conn.execute(
            "insert into goals(id, kind, text, rubric) values (%s,%s,%s,%s) "
            "on conflict (id) do update set kind=excluded.kind, "
            "text=excluded.text, rubric=excluded.rubric",
            (g.id, g.kind, g.text, g.rubric),
        )
    for m in models:
        conn.execute(
            "insert into models(id, openrouter_id, tier, enabled) "
            "values (%s,%s,%s,%s) on conflict (id) do update set "
            "openrouter_id=excluded.openrouter_id, tier=excluded.tier, "
            "enabled=case when models.openrouter_id <> excluded.openrouter_id "
            "then excluded.enabled else models.enabled and excluded.enabled end",
            (m.id, m.openrouter_id, int(m.tier), m.enabled),
        )
