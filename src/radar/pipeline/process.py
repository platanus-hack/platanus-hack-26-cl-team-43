import logging
from collections.abc import Callable

import psycopg

from radar.llm.base import LLM, LLMError
from radar.llm.embed import Embedder
from radar.pipeline.dedupe import ingest_prompt
from radar.pipeline.extract import ExtractedPrompt, extract_chunks
from radar.testing.budget import can_spend

log = logging.getLogger(__name__)

LOCK_SQL = "select pg_advisory_xact_lock(hashtext('radar_ingest'))"
MAX_PROCESS_ATTEMPTS = 3


def _mark_processed(conn: psycopg.Connection, raw_item_id: int) -> None:
    conn.execute("update raw_items set processed_at = now() where id = %s", (raw_item_id,))


def _record_crash(conn: psycopg.Connection, raw_item_id: int) -> None:
    """Count a non-LLM failure; give up on the item after MAX_PROCESS_ATTEMPTS. Commits."""
    row = conn.execute(
        "update raw_items set process_attempts = process_attempts + 1 where id = %s "
        "returning process_attempts", (raw_item_id,)).fetchone()
    if row and row["process_attempts"] >= MAX_PROCESS_ATTEMPTS:
        _mark_processed(conn, raw_item_id)
        log.error("raw_item %s failed processing %s times, giving up on it",
                  raw_item_id, row["process_attempts"])
    conn.commit()


def process_pending_raw_items(conn: psycopg.Connection, llm: LLM, embedder: Embedder,
                              model: str, on_new_candidate: Callable[[int], None],
                              limit: int = 50, *, budget_usd: float | None = None) -> int:
    """Extract + dedupe unprocessed raw items, one transaction each.

    Watchlist items go first (fresh pushes must not starve behind backlog), then oldest.

    Returns the number of items marked processed. A retryable or auth LLM error rolls
    back the current item (left unprocessed) and stops the batch. Other non-retryable
    errors keep the prompts of chunks extracted before the error and mark it processed.
    Extraction cost goes to llm_spend; with budget_usd set, the batch stops (item left
    unprocessed) once the daily budget is spent. Any other exception rolls back, counts
    an attempt on the item (given up after MAX_PROCESS_ATTEMPTS) and is re-raised.
    """
    ids = [r["id"] for r in conn.execute(
        "select id from raw_items where processed_at is null "
        "order by (source = 'github_watchlist') desc, fetched_at, id limit %s", (limit,))]
    conn.commit()
    done = 0
    for raw_id in ids:
        if budget_usd is not None and not can_spend(conn, budget_usd):
            conn.rollback()
            log.info("daily budget spent; leaving remaining raw items for later")
            return done
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
                cost = 0.0
                try:
                    for chunk_prompts, chunk_cost in extract_chunks(llm, model, text):
                        prompts.extend(chunk_prompts)
                        cost += chunk_cost
                except LLMError as e:
                    if e.retryable or e.kind == "auth":
                        raise
                    log.warning("extraction failed for raw_item %s, keeping %d prompts "
                                "from earlier chunks: %s", raw_id, len(prompts), e)
                conn.execute("insert into llm_spend(kind, cost_usd) values ('extract', %s)",
                             (cost,))
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
            _record_crash(conn, raw_id)
            raise
        done += 1
        for cid in new_ids:
            on_new_candidate(cid)
    return done
