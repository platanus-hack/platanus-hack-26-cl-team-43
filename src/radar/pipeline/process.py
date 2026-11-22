import logging
from collections.abc import Callable

import psycopg

from radar.llm.base import LLM, LLMError
from radar.llm.embed import Embedder
from radar.pipeline.dedupe import ingest_prompt
from radar.pipeline.extract import ExtractedPrompt, extract_chunks

log = logging.getLogger(__name__)

LOCK_SQL = "select pg_advisory_xact_lock(hashtext('radar_ingest'))"


def _mark_processed(conn: psycopg.Connection, raw_item_id: int) -> None:
    conn.execute("update raw_items set processed_at = now() where id = %s", (raw_item_id,))


def process_pending_raw_items(conn: psycopg.Connection, llm: LLM, embedder: Embedder,
                              model: str, on_new_candidate: Callable[[int], None],
                              limit: int = 50) -> int:
    """Extract + dedupe unprocessed raw items, one transaction each.

    Watchlist items go first (fresh pushes must not starve behind backlog), then oldest.

    Returns the number of items marked processed. A retryable or auth LLM error rolls
    back the current item (left unprocessed) and stops the batch. Other non-retryable
    errors keep the prompts of chunks extracted before the error and mark it processed.
    """
    ids = [r["id"] for r in conn.execute(
        "select id from raw_items where processed_at is null "
        "order by (source = 'github_watchlist') desc, fetched_at, id limit %s", (limit,))]
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
                prompts: list[ExtractedPrompt] = []
                try:
                    for chunk_prompts, _cost in extract_chunks(llm, model, text):
                        prompts.extend(chunk_prompts)
                except LLMError as e:
                    if e.retryable or e.kind == "auth":
                        raise
                    log.warning("extraction failed for raw_item %s, keeping %d prompts "
                                "from earlier chunks: %s", raw_id, len(prompts), e)
                for p in prompts:
                    out = ingest_prompt(conn, embedder, p, raw_id, item["seen_at"])
                    if out.novelty in ("novel", "variant"):
                        new_ids.append(out.candidate_id)
            _mark_processed(conn, raw_id)
            conn.commit()
        except LLMError as e:
            conn.rollback()
            log.warning("LLM error on raw_item %s (retryable or auth), stopping batch: %s",
                        raw_id, e)
            return done
        except Exception:
            conn.rollback()
            raise
        done += 1
        for cid in new_ids:
            on_new_candidate(cid)
    return done
