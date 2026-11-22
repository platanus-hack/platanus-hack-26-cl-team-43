import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg
from pgvector import Vector

from radar.llm.embed import Embedder
from radar.pipeline.extract import ExtractedPrompt

DUPLICATE_AT = 0.95
VARIANT_AT = 0.85
GOAL = "{GOAL}"

Novelty = Literal["duplicate", "variant", "novel", "known"]


@dataclass
class IngestOutcome:
    candidate_id: int
    novelty: Novelty


def normalize(text: str, goal_slot: str | None) -> str:
    out = text
    if goal_slot:
        out = out.replace(goal_slot, "\0")
    out = re.sub(r"\s+", " ", out.lower()).strip()
    return out.replace("\0", GOAL)


def classify(similarity: float | None) -> Literal["duplicate", "variant", "novel"]:
    if similarity is None or similarity < VARIANT_AT:
        return "novel"
    return "duplicate" if similarity >= DUPLICATE_AT else "variant"


def _add_sighting(conn: psycopg.Connection, candidate_id: int, raw_item_id: int,
                  seen_at: datetime) -> None:
    conn.execute(
        "insert into sightings (candidate_id, raw_item_id, seen_at) values (%s, %s, %s) "
        "on conflict do nothing", (candidate_id, raw_item_id, seen_at))


def ingest_prompt(conn: psycopg.Connection, embedder: Embedder, p: ExtractedPrompt,
                  raw_item_id: int, seen_at: datetime, known: bool = False,
                  embedding: list[float] | None = None) -> IngestOutcome:
    """Dedupe one prompt against all candidates. Runs in the caller's transaction."""
    template = normalize(p.text, p.goal_slot)
    vec = Vector(embedding if embedding is not None else embedder.embed([template])[0])
    nearest = conn.execute(
        "select id, family_id, 1 - (embedding <=> %s) as sim from candidates "
        "where embedding is not null order by embedding <=> %s limit 1",
        (vec, vec)).fetchone()
    kind = classify(nearest["sim"] if nearest else None)

    if kind == "duplicate":
        cid = nearest["id"]
        _add_sighting(conn, cid, raw_item_id, seen_at)
        conn.execute("update candidates set first_seen_at = least(first_seen_at, %s) "
                     "where id = %s", (seen_at, cid))
        return IngestOutcome(cid, "duplicate")

    if kind == "variant":
        family_id = nearest["family_id"]
    else:
        label = p.technique[0] if p.technique else "other"
        family_id = conn.execute(
            "insert into families (label, first_seen_at) values (%s, %s) returning id",
            (label, seen_at)).fetchone()["id"]
    novelty: Novelty = "known" if known else kind
    cid = conn.execute(
        """insert into candidates (family_id, text, template, technique, targets, format,
               goal_slot, embedded_goal, embedded_goal_severity, template_severity,
               embedding, novelty, first_seen_at)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
        (family_id, p.text, template, p.technique, p.targets, p.format, p.goal_slot,
         p.embedded_goal, p.embedded_goal_severity, p.template_severity, vec, novelty,
         seen_at)).fetchone()["id"]
    _add_sighting(conn, cid, raw_item_id, seen_at)
    return IngestOutcome(cid, novelty)
