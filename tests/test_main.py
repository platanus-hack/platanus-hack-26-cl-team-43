from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from radar import main
from radar.alerts.render import AlertMessage
from radar.alerts.rules import AlertDecision
from fakes import FakeEmbedder, FakeLLM

COLLECTORS = {"github_discovery", "github_watchlist", "github_code_search",
              "hf_datasets", "papers"}


def _settings():
    return SimpleNamespace(
        github_token="gh", hf_token=None, openrouter_api_key="or",
        extractor_model="x/extractor", judge_model="x/judge", base_url="http://radar.test",
    )


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.closed = False

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def sched():
    s, trigger = main.build_scheduler(lambda: FakeConn(), _settings(),
                                      llm=FakeLLM(), embedder=FakeEmbedder())
    yield s, trigger
    if s.running:
        s.shutdown(wait=False)


def test_build_collectors_returns_the_five_sources():
    cfg = main.load_sources()
    cs = main.build_collectors(FakeConn(), _settings(), cfg)
    assert {c.name for c in cs} == COLLECTORS


def test_scheduler_has_all_jobs(sched):
    s, _ = sched
    jobs = {j.id: j for j in s.get_jobs()}
    assert set(jobs) >= COLLECTORS | {"process_raw", "digest"}
    assert jobs["github_watchlist"].max_instances == 1
    assert jobs["github_watchlist"].coalesce is True
    assert jobs["github_watchlist"].trigger.interval == timedelta(minutes=10)
    assert jobs["process_raw"].trigger.interval == timedelta(minutes=1)
    digest = str(jobs["digest"].trigger)
    assert "hour='9'" in digest and "minute='0'" in digest
    assert str(jobs["digest"].trigger.timezone) == "America/Santiago"


def test_trigger_poll_moves_next_run_to_now(sched):
    s, trigger = sched
    s.start(paused=True)
    s.modify_job("papers", next_run_time=datetime.now(UTC) + timedelta(hours=5))
    trigger("papers")
    nrt = s.get_job("papers").next_run_time
    assert abs((nrt - datetime.now(UTC)).total_seconds()) < 5


def test_job_wrapper_survives_and_closes(monkeypatch):
    conns = []

    def factory():
        c = FakeConn()
        conns.append(c)
        return c

    def boom(conn):
        raise RuntimeError("db gone")

    main._run_job(factory, "x", boom)  # must not raise
    assert conns[0].closed

    def bad_factory():
        raise RuntimeError("cannot connect")

    main._run_job(bad_factory, "x", boom)  # must not raise either


def test_on_tier_done_dispatches_alert(monkeypatch):
    conn = FakeConn()
    d = AlertDecision(candidate_id=7, tier=1, severity=2, newly_broken=[("gpt", 0.8)])
    msg = AlertMessage(subject="s", text="t")
    seen = {}
    monkeypatch.setattr(main, "evaluate_alerts",
                        lambda c, cid, tier: seen.setdefault("eval", (c, cid, tier)) and d)
    monkeypatch.setattr(main, "render_alert",
                        lambda c, dec, base: seen.setdefault("render", (dec, base)) and msg)
    monkeypatch.setattr(main, "dispatch",
                        lambda c, s, dec, m: seen.setdefault("dispatch", (dec, m)) and 1)
    on_tier_done = main.make_on_tier_done(lambda: conn, _settings())
    on_tier_done(7, 1)
    assert seen["eval"] == (conn, 7, 1)
    assert seen["render"] == (d, "http://radar.test")
    assert seen["dispatch"] == (d, msg)
    assert conn.commits == 1 and conn.closed


def test_on_tier_done_without_decision_does_not_dispatch(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(main, "evaluate_alerts", lambda c, cid, tier: None)
    monkeypatch.setattr(main, "dispatch", lambda *a: pytest.fail("should not dispatch"))
    main.make_on_tier_done(lambda: conn, _settings())(7, 2)
    assert conn.closed


def test_seed_watchlist_inserts_seed_repos(conn):
    main.seed_watchlist(conn, {"seed_watchlist": ["a/b", "c/d"]})
    conn.execute("update watch_repos set status='muted' where full_name='a/b'")
    conn.commit()
    main.seed_watchlist(conn, {"seed_watchlist": ["a/b", "c/d"]})
    rows = {r["full_name"]: r["status"]
            for r in conn.execute("select full_name, status from watch_repos")}
    assert rows == {"a/b": "muted", "c/d": "seed"}
