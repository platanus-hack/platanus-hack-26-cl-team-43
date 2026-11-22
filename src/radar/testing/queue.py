"""Priority job queue for model tests, stored in test_jobs.

Functions here do not commit unless noted (claim_next commits so the row
lock is short); the caller owns the transaction.
"""
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from radar.testing.assemble import CandidateRow, testability

MAX_ATTEMPTS = 5
STALE_LOCK = timedelta(minutes=15)


@dataclass
class Job:
    id: int
    candidate_id: int
    model_id: str
    tier: int
    stage: Literal["screen", "confirm"]
    attempts: int


def priority(tier: int, novelty: str | None, source: str | None) -> int:
    p = 100 if tier == 1 else 0
    p += {"novel": 20, "variant": 10}.get(novelty or "", 0)
    p += 5 if source == "github_watchlist" else 0
    return p


def _first_source(conn, candidate_id: int) -> str | None:
    row = conn.execute(
        "select r.source from sightings s join raw_items r on r.id = s.raw_item_id "
        "where s.candidate_id = %s order by s.seen_at, s.raw_item_id limit 1",
        (candidate_id,),
    ).fetchone()
    return row["source"] if row else None


def enqueue_for_candidate(conn, candidate_id: int) -> int:
    """Queue a screen job per enabled model; 0 (and untested_reason) if untestable.

    Known candidates (seed corpus) are never tested: 0, test_status untouched.
    """
    row = conn.execute("select * from candidates where id = %s", (candidate_id,)).fetchone()
    if row["novelty"] == "known":
        return 0
    reason = testability(CandidateRow.from_row(row))
    if reason is not None:
        conn.execute(
            "update candidates set test_status = 'untested', untested_reason = %s where id = %s",
            (reason, candidate_id),
        )
        return 0
    source = _first_source(conn, candidate_id)
    n = 0
    for m in conn.execute("select id, tier from models where enabled order by id").fetchall():
        inserted = conn.execute(
            "insert into test_jobs(candidate_id, model_id, stage, priority) "
            "select %s, %s, 'screen', %s where not exists ("
            " select 1 from test_jobs where candidate_id = %s and model_id = %s"
            " and stage = 'screen')",
            (candidate_id, m["id"], priority(m["tier"], row["novelty"], source),
             candidate_id, m["id"]),
        ).rowcount
        n += inserted
    if n:
        conn.execute(
            "update candidates set test_status = 'queued', untested_reason = null where id = %s",
            (candidate_id,),
        )
    return n


def has_open_jobs(conn, candidate_id: int, tier: int | None = None) -> bool:
    row = conn.execute(
        "select exists (select 1 from test_jobs j join models m on m.id = j.model_id "
        "where j.candidate_id = %s and j.status in ('queued','running') "
        "and (%s::int is null or m.tier = %s::int)) as open",
        (candidate_id, tier, tier),
    ).fetchone()
    return row["open"]


def refresh_candidate_status(conn, candidate_id: int) -> None:
    if not has_open_jobs(conn, candidate_id):
        conn.execute("update candidates set test_status = 'tested' where id = %s",
                     (candidate_id,))


def _fail_disabled_model_jobs(conn) -> set[tuple[int, int]]:
    """Fail queued jobs of disabled models; returns the (candidate_id, tier) pairs touched."""
    rows = conn.execute(
        "update test_jobs j set status = 'failed', locked_at = null from models m "
        "where m.id = j.model_id and not m.enabled and j.status = 'queued' "
        "returning j.candidate_id, m.tier"
    ).fetchall()
    for cid in {r["candidate_id"] for r in rows}:
        refresh_candidate_status(conn, cid)
    return {(r["candidate_id"], r["tier"]) for r in rows}


def claim_next(conn, closed: set[tuple[int, int]] | None = None) -> Job | None:
    """Lock and mark running the next due job (also reclaims stale running jobs). Commits.

    Jobs of disabled models are failed on the way; pass `closed` to collect the
    (candidate_id, tier) pairs they belonged to.
    """
    swept = _fail_disabled_model_jobs(conn)
    if closed is not None:
        closed |= swept
    row = conn.execute(
        "select j.id, j.candidate_id, j.model_id, m.tier, j.stage, j.attempts "
        "from test_jobs j join models m on m.id = j.model_id "
        "where m.enabled and ("
        " (j.status = 'queued' and (j.not_before is null or j.not_before <= now()))"
        " or (j.status = 'running' and j.locked_at < now() - %s)) "
        "order by j.priority desc, j.id asc limit 1 for update of j skip locked",
        (STALE_LOCK,),
    ).fetchone()
    if row is None:
        conn.commit()
        return None
    conn.execute("update test_jobs set status = 'running', locked_at = now() where id = %s",
                 (row["id"],))
    conn.commit()
    return Job(id=row["id"], candidate_id=row["candidate_id"], model_id=row["model_id"],
               tier=row["tier"], stage=row["stage"], attempts=row["attempts"])


def _close(conn, job_id: int, status: str) -> None:
    row = conn.execute(
        "update test_jobs set status = %s, locked_at = null where id = %s returning candidate_id",
        (status, job_id),
    ).fetchone()
    refresh_candidate_status(conn, row["candidate_id"])


def finish(conn, job_id: int) -> None:
    _close(conn, job_id, "done")


def fail(conn, job_id: int) -> None:
    _close(conn, job_id, "failed")


def release(conn, job_id: int) -> None:
    """Put a running job back in the queue untouched (e.g. budget exhausted)."""
    conn.execute("update test_jobs set status = 'queued', locked_at = null where id = %s",
                 (job_id,))


def requeue(conn, job_id: int, delay: timedelta, *, count_attempt: bool = True) -> None:
    """Retry later with attempts+1; once attempts reaches MAX_ATTEMPTS the job fails.

    count_attempt=False retries without touching attempts (a global problem, not the job's).
    """
    inc = 1 if count_attempt else 0
    row = conn.execute(
        "update test_jobs set attempts = attempts + %s, locked_at = null, "
        "not_before = now() + %s, "
        "status = case when attempts + %s >= %s then 'failed' else 'queued' end "
        "where id = %s returning candidate_id, status",
        (inc, delay, inc, MAX_ATTEMPTS, job_id),
    ).fetchone()
    if row["status"] == "failed":
        refresh_candidate_status(conn, row["candidate_id"])
