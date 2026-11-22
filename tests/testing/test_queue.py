from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tester_helpers import (
    T1, T2, insert_job, job_status, seed_candidate, seed_config, seed_results_cost,
)

from radar.testing.budget import ProviderRateLimiter, can_spend, spent_today
from radar.testing.queue import (
    claim_next, enqueue_for_candidate, finish, priority, requeue,
)

SCL = ZoneInfo("America/Santiago")


# --- priority / enqueue ---------------------------------------------------

def test_priority_values():
    assert priority(1, "novel", "github_watchlist") == 125
    assert priority(1, "variant", "reddit") == 110
    assert priority(2, "novel", "reddit") == 20
    assert priority(2, "known", "reddit") == 0
    assert priority(2, None, "github_watchlist") == 5


def test_enqueue_creates_screen_job_per_enabled_model(conn):
    seed_config(conn)
    conn.execute("insert into models(id, openrouter_id, tier, enabled) values ('off','x/y',1,false)")
    cid = seed_candidate(conn, novelty="variant", source="github_watchlist")
    assert enqueue_for_candidate(conn, cid) == 2
    conn.commit()
    jobs = {r["model_id"]: r for r in conn.execute("select * from test_jobs order by id")}
    assert set(jobs) == {"t1", "t2"}
    assert jobs["t1"]["priority"] == 115 and jobs["t2"]["priority"] == 15
    assert all(j["stage"] == "screen" and j["status"] == "queued" for j in jobs.values())
    c = conn.execute("select test_status from candidates where id=%s", (cid,)).fetchone()
    assert c["test_status"] == "queued"


def test_enqueue_uses_earliest_sighting_source(conn):
    seed_config(conn)
    cid = seed_candidate(conn, source="reddit", novelty=None)
    rid = conn.execute("insert into raw_items(source, external_id, content_hash) "
                       "values ('github_watchlist','later','h') returning id").fetchone()["id"]
    conn.execute("insert into sightings(candidate_id, raw_item_id, seen_at) "
                 "values (%s,%s, now() + interval '1 day')", (cid, rid))
    enqueue_for_candidate(conn, cid)
    prios = {r["priority"] for r in conn.execute("select priority from test_jobs")}
    assert prios == {100, 0}


def test_untestable_candidate_gets_no_jobs(conn):
    seed_config(conn)
    cid = seed_candidate(conn, fmt="multi_turn")
    assert enqueue_for_candidate(conn, cid) == 0
    conn.commit()
    assert conn.execute("select count(*) n from test_jobs").fetchone()["n"] == 0
    c = conn.execute("select * from candidates where id=%s", (cid,)).fetchone()
    assert c["test_status"] == "untested" and c["untested_reason"] == "multi-turn format"


# --- claim ----------------------------------------------------------------

def test_tier1_claimed_before_tier2(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    enqueue_for_candidate(conn, cid)
    conn.commit()
    first = claim_next(conn)
    second = claim_next(conn)
    assert (first.model_id, first.tier, first.stage) == ("t1", 1, "screen")
    assert (second.model_id, second.tier) == ("t2", 2)
    assert claim_next(conn) is None
    assert job_status(conn, first.id) == "running"
    assert conn.execute("select locked_at from test_jobs where id=%s",
                        (first.id,)).fetchone()["locked_at"] is not None


def test_claim_orders_by_id_on_ties_and_respects_not_before(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    j1 = insert_job(conn, cid, "t2", priority=5)
    j2 = insert_job(conn, cid, "t2", priority=5)
    conn.execute("update test_jobs set not_before = now() + interval '1 hour' where id=%s", (j1,))
    conn.commit()
    assert claim_next(conn).id == j2
    assert claim_next(conn) is None


def test_claim_is_skip_locked(conn):
    from conftest import TEST_DATABASE_URL
    from radar import db
    seed_config(conn)
    cid = seed_candidate(conn)
    j1 = insert_job(conn, cid, "t1", priority=10)
    j2 = insert_job(conn, cid, "t1", priority=5)
    other = db.connect(TEST_DATABASE_URL)
    try:
        other.execute("select id from test_jobs where id=%s for update", (j1,))
        assert claim_next(conn).id == j2
    finally:
        other.rollback()
        other.close()


def test_claim_fails_jobs_for_disabled_models(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    j1 = insert_job(conn, cid, "t1", priority=50)
    conn.execute("update models set enabled=false where id='t1'")
    conn.commit()
    assert claim_next(conn) is None
    assert job_status(conn, j1) == "failed"


def test_claim_reclaims_stale_running_jobs(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    stale = insert_job(conn, cid, "t1", status="running")
    fresh = insert_job(conn, cid, "t1", status="running")
    conn.execute("update test_jobs set locked_at = now() - interval '16 minutes' where id=%s", (stale,))
    conn.execute("update test_jobs set locked_at = now() - interval '5 minutes' where id=%s", (fresh,))
    conn.commit()
    job = claim_next(conn)
    assert job.id == stale
    assert claim_next(conn) is None


# --- finish / requeue -----------------------------------------------------

def test_finish_marks_done_and_candidate_tested_when_no_open_jobs(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    enqueue_for_candidate(conn, cid)
    conn.commit()
    a, b = claim_next(conn), claim_next(conn)
    finish(conn, a.id)
    conn.commit()
    status = lambda: conn.execute("select test_status from candidates where id=%s",
                                  (cid,)).fetchone()["test_status"]
    assert job_status(conn, a.id) == "done" and status() == "queued"
    finish(conn, b.id)
    conn.commit()
    assert status() == "tested"


def test_requeue_sets_backoff_and_fails_after_five_attempts(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    jid = insert_job(conn, cid, "t1", status="running", attempts=0)
    requeue(conn, jid, timedelta(minutes=1))
    conn.commit()
    row = conn.execute("select *, not_before > now() + interval '50 seconds' later "
                       "from test_jobs where id=%s", (jid,)).fetchone()
    assert row["status"] == "queued" and row["attempts"] == 1 and row["later"]
    assert row["locked_at"] is None
    conn.execute("update test_jobs set attempts=4 where id=%s", (jid,))
    requeue(conn, jid, timedelta(minutes=16))
    conn.commit()
    row = conn.execute("select * from test_jobs where id=%s", (jid,)).fetchone()
    assert row["status"] == "failed" and row["attempts"] == 5


# --- budget ---------------------------------------------------------------

def test_spent_today_sums_results_and_baselines_since_santiago_midnight(conn):
    seed_config(conn)
    seed_results_cost(conn, 1.5)
    midnight = datetime.now(SCL).replace(hour=0, minute=0, second=0, microsecond=0)
    conn.execute("update results set created_at=%s", (midnight + timedelta(seconds=1),))
    jid = conn.execute("select id from test_jobs").fetchone()["id"]
    conn.execute("insert into results(job_id, goal_id, verdict, cost_usd, created_at) "
                 "values (%s,'canary_token','refused',100,%s)",
                 (jid, midnight - timedelta(seconds=1)))
    today = midnight.date()
    conn.execute("insert into baselines(model_id, goal_id, date, verdict, cost_usd) "
                 "values ('t1','canary_token',%s,'refused',0.5)", (today,))
    conn.execute("insert into baselines(model_id, goal_id, date, verdict, cost_usd) "
                 "values ('t1','policy_insult',%s,'refused',7)", (today - timedelta(days=1),))
    conn.commit()
    assert spent_today(conn) == 2.0
    assert can_spend(conn, 2.01) is True
    assert can_spend(conn, 2.0) is False


def test_spent_today_includes_llm_spend_since_santiago_midnight(conn):
    midnight = datetime.now(SCL).replace(hour=0, minute=0, second=0, microsecond=0)
    conn.execute("insert into llm_spend(kind, cost_usd, created_at) values "
                 "('extract', 0.75, %s), ('extract', 9, %s)",
                 (midnight + timedelta(seconds=1), midnight - timedelta(seconds=1)))
    conn.commit()
    assert spent_today(conn) == 0.75


def test_spent_today_empty_is_zero(conn):
    assert spent_today(conn) == 0.0


# --- rate limiter -----------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def test_rate_limiter_sliding_window_per_provider():
    clock = FakeClock()
    lim = ProviderRateLimiter(max_per_minute=2, clock=clock, sleep=clock.sleep)
    lim.acquire("openai/gpt")
    clock.now += 10
    lim.acquire("openai/gpt-mini")
    lim.acquire("anthropic/claude")  # other provider: not limited
    assert clock.slept == []
    lim.acquire("openai/gpt")  # third openai call waits until the first ages out
    assert clock.slept == [50.0]
    assert clock.now == 1060.0


def test_enqueue_known_candidate_is_noop(conn):
    seed_config(conn)
    cid = seed_candidate(conn, novelty="known")
    conn.execute("update candidates set test_status = null where id=%s", (cid,))
    conn.commit()
    assert enqueue_for_candidate(conn, cid) == 0
    assert conn.execute("select count(*) n from test_jobs").fetchone()["n"] == 0
    assert conn.execute("select test_status from candidates where id=%s",
                        (cid,)).fetchone()["test_status"] is None
