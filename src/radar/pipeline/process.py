import logging
from collections.abc import Callable

import psycopg

from radar.llm.base import LLM, LLMError
from radar.llm.embed import Embedder
from radar.pipeline.dedupe import ingest_prompt
from radar.pipeline.extract import extract

log = logging.getLogger(__name__)

LOCK_SQL = "select pg_advisory_xact_lock(hashtext('radar_ingest'))"


def _mark_processed(conn: psycopg.Connection, raw_item_id: int) -> None:
    conn.execute("update raw_items set processed_at = now() where id = %s", (raw_item_id,))


def process_pending_raw_items(conn: psycopg.Connection, llm: LLM, embedder: Embedder,
                              model: str, on_new_candidate: Callable[[int], None],
                              limit: int = 50) -> int:
    """Extract + dedupe unprocessed raw items, oldest first, one transaction each.

    Returns the number of items marked processed. A retryable LLM error rolls back the
    current item (left unprocessed) and stops the batch.
    """
    ids = [r["id"] for r in conn.execute(
        "select id from raw_items where processed_at is null "
        "order by fetched_at, id limit %s", (limit,))]
    conn.commit()
    done = 0
    for raw_id in ids:
        new_ids: list[int] = []
        try:
            conn.execute(LOCK_SQL)
            item = conn.execute(
                "select id, kind, content_text, coalesce(published_at, fetched_at) seen_at "
                "from raw_items where id = %s and processed_at is null", (raw_id,)).fetchone()
            if item is None:  # processed by a concurrent run while we waited
                conn.rollback()
                continue
            text = item["content_text"] or ""
            if item["kind"] == "text" and text.strip():
                try:
                    prompts = extract(llm, model, text)
                except LLMError as e:
                    if e.retryable:
                        raise
                    log.warning("extraction failed for raw_item %s, skipping: %s", raw_id, e)
                    prompts = []
                for p in prompts:
                    out = ingest_prompt(conn, embedder, p, raw_id, item["seen_at"])
                    if out.novelty in ("novel", "variant"):
                        new_ids.append(out.candidate_id)
            _mark_processed(conn, raw_id)
            conn.commit()
        except LLMError as e:
            conn.rollback()
            log.warning("retryable LLM error on raw_item %s, stopping batch: %s", raw_id, e)
            return done
        except Exception:
            conn.rollback()
            raise
        done += 1
        for cid in new_ids:
            on_new_candidate(cid)
    return done
