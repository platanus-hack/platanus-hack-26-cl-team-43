"""Seed the candidates table with a known public jailbreak corpus (novelty='known')."""
import hashlib
import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from itertools import batched

import httpx
import psycopg

from radar.llm.embed import Embedder
from radar.pipeline.dedupe import ingest_prompt, normalize
from radar.pipeline.extract import ExtractedPrompt

log = logging.getLogger(__name__)

SOURCE = "seed_corpus"
DATASET = "TrustAIRLab/in-the-wild-jailbreak-prompts"
CONFIG = "jailbreak_2023_12_25"
SPLIT = "train"
COLUMN = "prompt"
DATASET_URL = f"https://huggingface.co/datasets/{DATASET}"
ROWS_API = "https://datasets-server.huggingface.co/rows"
PAGE = 100
BATCH = 64


def fetch_rows(http: httpx.Client, hf_token: str | None = None) -> Iterator[str]:
    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
    offset = 0
    while True:
        resp = http.get(ROWS_API, headers=headers, params={
            "dataset": DATASET, "config": CONFIG, "split": SPLIT,
            "offset": offset, "length": PAGE})
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("rows", [])
        for r in rows:
            value = r.get("row", {}).get(COLUMN)
            if isinstance(value, str):
                yield value
        offset += len(rows)
        if not rows or offset >= data.get("num_rows_total", 0):
            return


def _known_prompt(text: str) -> ExtractedPrompt:
    return ExtractedPrompt(text=text, confidence=1.0, technique=["other"], targets=[],
                           format="single_turn", goal_slot=None, embedded_goal=None,
                           embedded_goal_severity=None, template_severity="low")


def seed_known(conn: psycopg.Connection, embedder: Embedder, rows: Iterable[str],
               source_url: str) -> int:
    """Ingest rows as known candidates; returns rows ingested (already-seeded rows skipped)."""
    now = datetime.now(UTC)
    total = 0
    texts = (t for t in rows if t and t.strip())
    for batch in batched(texts, BATCH):
        vectors = embedder.embed([normalize(t, None) for t in batch])
        for text, vec in zip(batch, vectors):
            h = hashlib.sha256(text.encode()).hexdigest()
            row = conn.execute(
                """insert into raw_items (source, external_id, url, content_text,
                       content_hash, kind, fetched_at, processed_at)
                   values (%s, %s, %s, %s, %s, 'text', %s, %s)
                   on conflict (source, external_id, content_hash) do nothing returning id""",
                (SOURCE, h, source_url, text, h, now, now)).fetchone()
            if row is None:
                continue
            ingest_prompt(conn, embedder, _known_prompt(text), row["id"], now,
                          known=True, embedding=vec)
            total += 1
        conn.commit()
        log.info("seed corpus: %d prompts ingested", total)
    return total


def run() -> int:
    """Entry point for `radar seed-corpus`."""
    from radar import db
    from radar.settings import Settings

    settings = Settings()
    conn = db.connect(settings.database_url)
    try:
        with httpx.Client(timeout=60) as http:
            return seed_known(conn, Embedder(), fetch_rows(http, settings.hf_token),
                              DATASET_URL)
    finally:
        conn.close()
