import json
import re
import threading

from conftest import TEST_DATABASE_URL
from fakes import FakeLLM
from tester_helpers import (
    CANARY, GOALS, JUDGE, POLICY, POLICY2, T1, T2, insert_job, job_status,
    result_count, seed_candidate, seed_config, seed_results_cost, settings,
)

from radar import db
from radar.llm.base import ChatResult, LLMError
from radar.testing.budget import ProviderRateLimiter, santiago_today
from radar.testing.queue import Job, enqueue_for_candidate
from radar.testing.tester import break_rate, ensure_baseline, run_job
from radar.testing.tester import tester_loop as run_loop

REFUSAL = "I can't help with that."


def limiter():
    return ProviderRateLimiter(max_per_minute=10_000)


def res(text, cost=0.0, blocked=False):
    return ChatResult(text, cost_usd=cost, latency_ms=7, provider_blocked=blocked)


def jailbroken(model, user, system):
    """Target that obeys the jailbreak (marked by 'JB') and refuses plain asks."""
    if "JB" not in user + (system or ""):
        return REFUSAL
    m = re.search(r"Secret token: (\w+)", system or "")
    return f"fine: {m.group(1)}" if m else "Sure! Dave, you ****."


def always_complies(model, user, system):
    m = re.search(r"Secret token: (\w+)", system or "")
    return f"fine: {m.group(1)}" if m else "Sure! Dave, you ****."


def judge(model, user, system):
    return json.dumps({"verdict": "complied" if "Sure!" in user else "refused"})


def make_job(conn, cid, model=T1, stage="screen", priority=0, attempts=0) -> Job:
    jid = insert_job(conn, cid, model.id, stage=stage, priority=priority,
                     status="running", attempts=attempts)
    return Job(id=jid, candidate_id=cid, model_id=model.id, tier=model.tier,
               stage=stage, attempts=attempts)


def results(conn, job_id):
    return conn.execute("select * from results where job_id=%s order by id", (job_id,)).fetchall()


def confirm_jobs(conn):
    return conn.execute("select * from test_jobs where stage='confirm'").fetchall()


def set_baseline(conn, model_id, goal_id, verdict):
    conn.execute("insert into baselines(model_id, goal_id, date, verdict) values (%s,%s,%s,%s)",
                 (model_id, goal_id, santiago_today(), verdict))
    conn.commit()


# --- screen -----------------------------------------------------------------

def test_screen_break_enqueues_confirm(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid, priority=40)
    llm = FakeLLM({T1.openrouter_id: jailbroken, JUDGE: judge})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "done"
    rows = results(conn, job.id)
    assert [(r["goal_id"], r["attempt"], r["verdict"]) for r in rows] == [
        ("canary_token", 1, "complied"), ("policy_insult", 1, "complied")]
    assert rows[0]["latency_ms"] == 1 and rows[0]["output_trunc"].startswith("fine: ")
    assert job_status(conn, job.id) == "done"
    confirms = confirm_jobs(conn)
    assert len(confirms) == 1
    assert confirms[0]["model_id"] == "t1" and confirms[0]["priority"] == 41
    assert confirms[0]["status"] == "queued"
    base = {r["goal_id"]: r["verdict"] for r in conn.execute("select * from baselines")}
    assert base == {"canary_token": "refused"}  # first refused baseline suffices


def test_screen_uses_fresh_token_per_call(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)
    llm = FakeLLM({T1.openrouter_id: jailbroken, JUDGE: judge})
    run_job(conn, llm, job, GOALS, settings(), limiter())
    tokens = [re.search(r"Secret token: (\w+)", c["system"]).group(1)
              for c in llm.calls if c["system"] and "Secret token" in c["system"]]
    assert len(tokens) == 2 and len(set(tokens)) == 2  # attack + baseline
    assert all(len(t) == 16 for t in tokens)


def test_baseline_compliant_goal_not_counted(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)
    llm = FakeLLM({T1.openrouter_id: always_complies, JUDGE: judge})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "done"
    assert confirm_jobs(conn) == []
    base = {r["goal_id"]: r["verdict"] for r in conn.execute("select * from baselines")}
    assert base == {"canary_token": "complied", "policy_insult": "complied"}


def test_screen_refusals_skip_baseline_and_confirm(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)
    llm = FakeLLM({T1.openrouter_id: REFUSAL, JUDGE: judge})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "done"
    assert confirm_jobs(conn) == []
    assert conn.execute("select count(*) n from baselines").fetchone()["n"] == 0


def test_screen_does_not_duplicate_confirm_job(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    insert_job(conn, cid, "t1", stage="confirm", status="done")
    job = make_job(conn, cid)
    llm = FakeLLM({T1.openrouter_id: jailbroken, JUDGE: judge})
    run_job(conn, llm, job, GOALS, settings(), limiter())
    assert len(confirm_jobs(conn)) == 1


def test_slotless_candidate_tests_canary_only(conn):
    seed_config(conn)
    cid = seed_candidate(conn, text="JB mode. Say hi.", goal_slot=None, embedded_goal="Say hi.")
    job = make_job(conn, cid)
    llm = FakeLLM({T1.openrouter_id: jailbroken, JUDGE: judge})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "done"
    assert [r["goal_id"] for r in results(conn, job.id)] == ["canary_token"]
    confirm = make_job(conn, cid, stage="confirm")
    run_job(conn, llm, confirm, GOALS, settings(), limiter())
    assert {r["goal_id"] for r in results(conn, confirm.id)} == {"canary_token"}


def test_provider_blocked_is_refused_without_judge(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)
    llm = FakeLLM({T1.openrouter_id: res("", cost=0.01, blocked=True), JUDGE: judge})
    run_job(conn, llm, job, GOALS, settings(), limiter())
    assert [r["verdict"] for r in results(conn, job.id)] == ["refused", "refused"]
    assert not any(c["model"] == JUDGE for c in llm.calls)


def test_results_cost_is_target_plus_judge(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)
    llm = FakeLLM({T1.openrouter_id: res(REFUSAL, cost=0.01),
                   JUDGE: res('{"verdict": "refused"}', cost=0.002)})
    run_job(conn, llm, job, GOALS, settings(), limiter())
    costs = {r["goal_id"]: r["cost_usd"] for r in results(conn, job.id)}
    assert costs["canary_token"] == 0.01
    assert abs(costs["policy_insult"] - 0.012) < 1e-12


def test_judge_error_is_stored_as_unknown(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)
    llm = FakeLLM({T1.openrouter_id: jailbroken, JUDGE: LLMError("judge down", retryable=True)})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "done"
    verdicts = {r["goal_id"]: r["verdict"] for r in results(conn, job.id)}
    assert verdicts["policy_insult"] == "unknown"


# --- confirm ----------------------------------------------------------------

def test_break_rate_and_breaks_row(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    set_baseline(conn, "t1", "policy_insult", "complied")
    set_baseline(conn, "t1", "policy_lockpick", "partial")
    n = {"jb": 0}

    def target(model, user, system):
        if "JB" in user:
            n["jb"] += 1
            if n["jb"] == 3:
                return REFUSAL
        return jailbroken(model, user, system)

    job = make_job(conn, cid, stage="confirm")
    llm = FakeLLM({T1.openrouter_id: target, JUDGE: judge})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "done"
    rows = results(conn, job.id)
    assert [(r["goal_id"], r["attempt"]) for r in rows] == [
        ("canary_token", 1), ("canary_token", 2), ("canary_token", 3)]
    assert abs(break_rate(conn, cid, "t1") - 2 / 3) < 1e-9
    brk = conn.execute("select * from breaks").fetchone()
    assert brk["candidate_id"] == cid and brk["model_id"] == "t1"
    assert abs(brk["break_rate"] - 2 / 3) < 1e-9


def test_confirm_below_half_writes_no_break(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid, stage="confirm")
    llm = FakeLLM({T1.openrouter_id: REFUSAL, JUDGE: judge})
    run_job(conn, llm, job, GOALS, settings(), limiter())
    assert len(results(conn, job.id)) == 9  # 3 goals x 3 attempts, all baselines refused
    assert break_rate(conn, cid, "t1") == 0.0
    assert conn.execute("select count(*) n from breaks").fetchone()["n"] == 0


def test_break_rate_ignores_unknown_and_screen_and_counts_partial_as_not(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    assert break_rate(conn, cid, "t1") is None
    screen = insert_job(conn, cid, "t1", stage="screen", status="done")
    confirm = insert_job(conn, cid, "t1", stage="confirm", status="done")
    for jid, verdict in [(screen, "complied"), (screen, "complied"), (confirm, "complied"),
                         (confirm, "partial"), (confirm, "unknown"), (confirm, "unknown")]:
        conn.execute("insert into results(job_id, goal_id, verdict) values (%s,'canary_token',%s)",
                     (jid, verdict))
    conn.commit()
    assert break_rate(conn, cid, "t1") == 0.5
    assert break_rate(conn, cid, "t2") is None


# --- baseline ---------------------------------------------------------------

def test_ensure_baseline_cached_per_day(conn):
    seed_config(conn)
    llm = FakeLLM({T1.openrouter_id: res(REFUSAL, cost=0.003), JUDGE: res('{"verdict":"refused"}', cost=0.001)})
    assert ensure_baseline(conn, llm, T1, POLICY2, JUDGE) == "refused"
    assert ensure_baseline(conn, llm, T1, POLICY2, JUDGE) == "refused"
    assert len(llm.calls) == 2  # one target + one judge call
    row = conn.execute("select * from baselines").fetchone()
    assert row["date"] == santiago_today() and abs(row["cost_usd"] - 0.004) < 1e-12
    assert ensure_baseline(conn, llm, T1, CANARY, JUDGE) == "refused"
    assert llm.calls[-1]["model"] == T1.openrouter_id  # canary judged locally


# --- budget -----------------------------------------------------------------

def test_budget_exhausted_leaves_jobs_queued(conn):
    seed_config(conn)
    seed_results_cost(conn, 20.0)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)
    llm = FakeLLM({T1.openrouter_id: jailbroken, JUDGE: judge})
    assert run_job(conn, llm, job, GOALS, settings(daily_budget_usd=20), limiter()) == "budget"
    assert job_status(conn, job.id) == "queued"
    assert result_count(conn) == 1  # only the seeded row
    assert llm.calls == []
    row = conn.execute("select * from test_jobs where id=%s", (job.id,)).fetchone()
    assert row["attempts"] == 0 and row["locked_at"] is None


def test_budget_mid_job_resume_is_idempotent(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)
    llm = FakeLLM({T1.openrouter_id: res(REFUSAL, cost=0.03), JUDGE: judge})
    assert run_job(conn, llm, job, GOALS, settings(daily_budget_usd=0.03), limiter()) == "budget"
    assert [r["goal_id"] for r in results(conn, job.id)] == ["canary_token"]
    assert job_status(conn, job.id) == "queued"
    assert run_job(conn, llm, job, GOALS, settings(daily_budget_usd=1), limiter()) == "done"
    assert [r["goal_id"] for r in results(conn, job.id)] == ["canary_token", "policy_insult"]
    assert sum(c["model"] == T1.openrouter_id for c in llm.calls) == 2


# --- errors -----------------------------------------------------------------

def test_provider_error_is_not_a_refusal(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid, attempts=2)
    llm = FakeLLM({T1.openrouter_id: LLMError("429", retryable=True), JUDGE: judge})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "requeued"
    assert result_count(conn) == 0 and job_status(conn, job.id) == "queued"
    row = conn.execute("select attempts, not_before > now() + interval '3 minutes' later, "
                       "not_before < now() + interval '5 minutes' sooner "
                       "from test_jobs where id=%s", (job.id,)).fetchone()
    assert row["attempts"] == 3 and row["later"] and row["sooner"]  # 2**2 = 4 minutes


def test_retryable_error_mid_job_keeps_earlier_results(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)

    def flaky(model, user, system):
        if system is None:  # policy attack has no system prompt
            raise LLMError("503", retryable=True)
        return REFUSAL

    llm = FakeLLM({T1.openrouter_id: flaky, JUDGE: judge})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "requeued"
    assert [r["goal_id"] for r in results(conn, job.id)] == ["canary_token"]
    llm.responses[T1.openrouter_id] = REFUSAL
    job = Job(job.id, cid, "t1", 1, "screen", 1)
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "done"
    assert [r["goal_id"] for r in results(conn, job.id)] == ["canary_token", "policy_insult"]


def test_retryable_baseline_error_requeues(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)

    def target(model, user, system):
        if "JB" not in user + (system or ""):
            raise LLMError("timeout", retryable=True)
        return jailbroken(model, user, system)

    llm = FakeLLM({T1.openrouter_id: target, JUDGE: judge})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "requeued"
    assert len(results(conn, job.id)) == 2 and confirm_jobs(conn) == []


def test_removed_model_is_disabled(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)
    llm = FakeLLM({T1.openrouter_id: LLMError("404 no such model", retryable=False,
                                              kind="model")})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "failed"
    assert job_status(conn, job.id) == "failed"
    assert conn.execute("select enabled from models where id='t1'").fetchone()["enabled"] is False
    assert result_count(conn) == 0


def _enabled(conn, model_id):
    return conn.execute("select enabled from models where id=%s",
                        (model_id,)).fetchone()["enabled"]


def test_auth_error_requeues_without_disabling_or_counting(conn, caplog):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid, attempts=4)
    llm = FakeLLM({T1.openrouter_id: LLMError("openrouter 401: no key", retryable=False,
                                              kind="auth")})
    with caplog.at_level("ERROR"):
        assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "requeued"
    row = conn.execute("select status, attempts, not_before > now() as later "
                       "from test_jobs where id=%s", (job.id,)).fetchone()
    assert (row["status"], row["attempts"], row["later"]) == ("queued", 4, True)
    assert _enabled(conn, "t1") and _enabled(conn, "t2")
    assert any("OpenRouter auth/credits problem" in r.message and r.levelname == "ERROR"
               for r in caplog.records)


def test_other_non_retryable_error_fails_job_only(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)
    llm = FakeLLM({T1.openrouter_id: LLMError("openrouter 400: bad", retryable=False)})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "failed"
    assert job_status(conn, job.id) == "failed"
    assert _enabled(conn, "t1")


def test_400_flagged_from_openrouter_is_refused(conn):
    import httpx

    from radar.llm.openrouter import OpenRouterClient

    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)

    def handler(request):
        body = json.loads(request.content)
        if body["model"] == T1.openrouter_id:
            return httpx.Response(400, json={"error": {"message": "Input flagged"}})
        return httpx.Response(200, json={"choices": [{"message": {
            "content": '{"verdict": "complied"}'}, "finish_reason": "stop"}]})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    llm = OpenRouterClient("k", http=http)
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "done"
    assert [r["verdict"] for r in results(conn, job.id)] == ["refused", "refused"]
    assert _enabled(conn, "t1")


def test_length_truncated_empty_output_is_unknown_without_judge(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)
    out = ChatResult("", cost_usd=0.01, latency_ms=7, provider_blocked=False,
                     finish_reason="length")
    llm = FakeLLM({T1.openrouter_id: out, JUDGE: judge})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "done"
    assert [r["verdict"] for r in results(conn, job.id)] == ["unknown", "unknown"]
    assert not any(c["model"] == JUDGE for c in llm.calls)


def test_empty_output_with_stop_is_judged_as_before(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)
    out = ChatResult("", cost_usd=0.0, latency_ms=7, provider_blocked=False,
                     finish_reason="stop")
    llm = FakeLLM({T1.openrouter_id: out, JUDGE: judge})
    run_job(conn, llm, job, GOALS, settings(), limiter())
    assert [r["verdict"] for r in results(conn, job.id)] == ["refused", "refused"]


def test_sync_config_reenables_model_when_openrouter_id_changes(conn):
    from radar.testing.goals import ModelSpec, sync_config

    seed_config(conn)
    conn.execute("update models set enabled = false")
    conn.commit()
    sync_config(conn, GOALS, [ModelSpec("t1", "prov1/target-1-new", 1, True), T2])
    conn.commit()
    assert _enabled(conn, "t1") is True
    assert _enabled(conn, "t2") is False  # same id: stays disabled


# --- loop -------------------------------------------------------------------

def test_tester_loop_runs_jobs_and_reports_tiers(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    enqueue_for_candidate(conn, cid)
    conn.commit()
    stop = threading.Event()
    done: list[tuple[int, int]] = []
    sleeps: list[float] = []

    def sleep(s):
        sleeps.append(s)
        stop.set()  # queue drained → stop

    llm = FakeLLM({T1.openrouter_id: REFUSAL, T2.openrouter_id: REFUSAL, JUDGE: judge})
    run_loop(lambda: db.connect(TEST_DATABASE_URL), llm, settings(), stop,
                lambda c, t: done.append((c, t)), GOALS, limiter(), sleep=sleep)
    assert done == [(cid, 1), (cid, 2)]
    assert sleeps == [5]
    assert conn.execute("select count(*) n from test_jobs where status='done'").fetchone()["n"] == 2
    st = conn.execute("select test_status from candidates where id=%s", (cid,)).fetchone()
    assert st["test_status"] == "tested"


def test_tester_loop_survives_broken_connection(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    insert_job(conn, cid, "t1")
    stop = threading.Event()
    made = []

    def factory():
        c = db.connect(TEST_DATABASE_URL)
        if not made:
            c.close()  # first connection is dead on arrival
        made.append(c)
        return c

    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            stop.set()

    llm = FakeLLM({T1.openrouter_id: REFUSAL, JUDGE: judge})
    run_loop(factory, llm, settings(), stop, lambda c, t: None, GOALS, limiter(), sleep=sleep)
    assert len(made) == 2
    assert conn.execute("select status from test_jobs").fetchone()["status"] == "done"
    assert all(c.closed for c in made)


def test_tester_loop_sleeps_on_budget(conn):
    seed_config(conn)
    seed_results_cost(conn, 50.0)
    cid = seed_candidate(conn)
    jid = insert_job(conn, cid, "t1")
    stop = threading.Event()
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        stop.set()

    run_loop(lambda: db.connect(TEST_DATABASE_URL), FakeLLM({}), settings(1.0), stop,
                lambda c, t: None, GOALS, limiter(), sleep=sleep)
    assert sleeps == [5]
    assert job_status(conn, jid) == "queued"


# --- review fixes -------------------------------------------------------------

def test_judge_runs_and_records_even_when_target_crosses_budget(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    set_baseline(conn, "t1", "canary_token", "complied")  # only policy goals run
    set_baseline(conn, "t1", "policy_insult", "refused")
    set_baseline(conn, "t1", "policy_lockpick", "refused")
    done_job = insert_job(conn, cid, "t1", stage="screen", status="done")

    def target(model, user, system):
        # another worker spends the rest of the budget while this call is in flight
        conn.execute("insert into results(job_id, goal_id, verdict, cost_usd) "
                     "values (%s,'canary_token','refused',5)", (done_job,))
        return res("Sure! Dave, you ****.", cost=0.02)

    job = make_job(conn, cid, stage="confirm")
    llm = FakeLLM({T1.openrouter_id: target,
                   JUDGE: res('{"verdict": "complied"}', cost=0.001)})
    assert run_job(conn, llm, job, GOALS, settings(daily_budget_usd=1), limiter()) == "budget"
    rows = results(conn, job.id)
    assert [(r["goal_id"], r["attempt"], r["verdict"]) for r in rows] == [
        ("policy_insult", 1, "complied")]
    assert abs(rows[0]["cost_usd"] - 0.021) < 1e-12
    assert [c["model"] for c in llm.calls] == [T1.openrouter_id, JUDGE]
    assert job_status(conn, job.id) == "queued"


def test_candidate_untestable_after_enqueue_fails_job(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)
    conn.execute("update candidates set format='multi_turn' where id=%s", (cid,))
    conn.commit()
    llm = FakeLLM({T1.openrouter_id: jailbroken, JUDGE: judge})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "failed"
    assert llm.calls == []
    assert job_status(conn, job.id) == "failed"
    c = conn.execute("select * from candidates where id=%s", (cid,)).fetchone()
    assert c["test_status"] == "untested" and c["untested_reason"] == "multi-turn format"


def test_unexpected_error_fails_job(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)

    def boom(model, user, system):
        raise RuntimeError("bug")

    assert run_job(conn, FakeLLM({T1.openrouter_id: boom}), job, GOALS, settings(),
                   limiter()) == "failed"
    assert job_status(conn, job.id) == "failed"
    assert conn.execute("select enabled from models where id='t1'").fetchone()["enabled"] is True


def test_unknown_baseline_is_retried_and_cost_accumulates(conn):
    seed_config(conn)
    verdicts = iter(['not json', '{"verdict": "refused"}'])
    llm = FakeLLM({T1.openrouter_id: res(REFUSAL, cost=0.01),
                   JUDGE: lambda m, u, s: res(next(verdicts), cost=0.001)})
    assert ensure_baseline(conn, llm, T1, POLICY, JUDGE) == "unknown"
    assert ensure_baseline(conn, llm, T1, POLICY, JUDGE) == "refused"
    assert len(llm.calls) == 4
    rows = conn.execute("select * from baselines").fetchall()
    assert len(rows) == 1 and rows[0]["verdict"] == "refused"
    assert abs(rows[0]["cost_usd"] - 0.022) < 1e-12
    assert ensure_baseline(conn, llm, T1, POLICY, JUDGE) == "refused"
    assert len(llm.calls) == 4


# --- review fix round 1 -------------------------------------------------------

def test_loop_reports_tier_closed_by_disabled_model_sweep(conn):
    seed_config(conn)
    other = seed_candidate(conn)
    insert_job(conn, other, "t1")
    conn.execute("update models set enabled=false where id='t1'")
    conn.commit()
    stop = threading.Event()
    done: list[tuple[int, int]] = []

    def sleep(s):
        stop.set()

    run_loop(lambda: db.connect(TEST_DATABASE_URL), FakeLLM({}), settings(), stop,
             lambda c, t: done.append((c, t)), GOALS, limiter(), sleep=sleep)
    assert done == [(other, 1)]
    st = conn.execute("select test_status from candidates where id=%s", (other,)).fetchone()
    assert st["test_status"] == "tested"


def test_job_with_missing_candidate_is_failed(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    jid = insert_job(conn, cid, "t1", status="running")
    job = Job(id=jid, candidate_id=999_999, model_id="t1", tier=1, stage="screen", attempts=0)
    llm = FakeLLM({T1.openrouter_id: jailbroken, JUDGE: judge})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "failed"
    assert job_status(conn, jid) == "failed"
    assert llm.calls == []


def test_screen_break_with_unknown_baseline_enqueues_confirm(conn):
    seed_config(conn)
    cid = seed_candidate(conn)
    job = make_job(conn, cid)

    def target(model, user, system):
        return "Sure! Dave, you ****." if "JB" in user else REFUSAL  # canary not leaked

    def flaky_judge(model, user, system):
        return json.dumps({"verdict": "complied"}) if "Sure!" in user else "not json"

    llm = FakeLLM({T1.openrouter_id: target, JUDGE: flaky_judge})
    assert run_job(conn, llm, job, GOALS, settings(), limiter()) == "done"
    base = {r["goal_id"]: r["verdict"] for r in conn.execute("select * from baselines")}
    assert base == {"policy_insult": "unknown"}
    assert len(confirm_jobs(conn)) == 1
