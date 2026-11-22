"""Process wiring: collectors + pipeline + digest on a scheduler, tester thread, web app."""
import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from radar.alerts.channels import dispatch, dispatch_digest
from radar.alerts.render import render_alert, render_digest
from radar.alerts.rules import evaluate_alerts
from radar.collectors.github_client import GitHubClient
from radar.collectors.github_code_search import GitHubCodeSearch
from radar.collectors.github_discovery import GitHubDiscovery
from radar.collectors.github_watchlist import GitHubWatchlist
from radar.collectors.hf_datasets import HFDatasets
from radar.collectors.papers import Papers
from radar.collectors.runner import run_collector
from radar.pipeline.process import process_pending_raw_items
from radar.testing.queue import enqueue_for_candidate

log = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("RADAR_CONFIG_DIR",
                                 Path(__file__).resolve().parents[2] / "config"))
COLLECTOR_CLASSES = (GitHubDiscovery, GitHubWatchlist, GitHubCodeSearch, HFDatasets, Papers)
TZ = "America/Santiago"
HTTP_TIMEOUT_S = 60


def load_sources(path: Path | None = None) -> dict:
    return yaml.safe_load((path or CONFIG_DIR / "sources.yaml").read_text())


def build_collectors(conn, settings, sources_cfg, http: httpx.Client | None = None) -> list:
    """All five collectors; `settings` only needs github_token and hf_token."""
    from huggingface_hub import HfApi

    http = http or httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True)
    gh = GitHubClient(settings.github_token, http)
    return [
        GitHubDiscovery(gh, conn, sources_cfg["discovery_queries"]),
        GitHubWatchlist(gh, conn),
        GitHubCodeSearch(gh, sources_cfg["code_search_markers"]),
        HFDatasets(HfApi(token=settings.hf_token), http, sources_cfg["hf_keywords"],
                   hf_token=settings.hf_token),
        Papers(conn, http),
    ]


def poll_once(conn, settings, sources_cfg, name: str) -> int:
    """Run collector `name` once; returns the number of inserted raw_items."""
    with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as http:
        by_name = {c.name: c for c in build_collectors(conn, settings, sources_cfg, http)}
        if name not in by_name:
            raise KeyError(f"unknown collector {name!r}; one of {sorted(by_name)}")
        return run_collector(conn, by_name[name])


def seed_watchlist(conn, sources_cfg) -> None:
    for full_name in sources_cfg.get("seed_watchlist") or []:
        conn.execute("insert into watch_repos (full_name, status) values (%s, 'seed') "
                     "on conflict do nothing", (full_name,))
    conn.commit()


def _run_job(conn_factory, name: str, fn: Callable[[object], object]) -> None:
    """Own connection per run; never raises so a broken DB cannot kill the scheduler."""
    conn = None
    try:
        conn = conn_factory()
        fn(conn)
    except Exception:
        log.exception("job %s failed", name)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                log.exception("job %s: closing connection failed", name)


def make_on_tier_done(conn_factory, settings) -> Callable[[int, int], None]:
    def on_tier_done(candidate_id: int, tier: int) -> None:
        def run(conn):
            d = evaluate_alerts(conn, candidate_id, tier)
            if d is not None:
                dispatch(conn, settings, d, render_alert(conn, d, settings.base_url))
            conn.commit()
        _run_job(conn_factory, f"on_tier_done({candidate_id},{tier})", run)
    return on_tier_done


def build_scheduler(conn_factory, settings, *, llm=None, embedder=None, sources_cfg=None
                    ) -> tuple[BackgroundScheduler, Callable[[str], None]]:
    from radar.llm.embed import Embedder
    from radar.llm.openrouter import OpenRouterClient

    llm = llm or OpenRouterClient(settings.openrouter_api_key)
    embedder = embedder or Embedder()
    sources_cfg = sources_cfg or load_sources()
    scheduler = BackgroundScheduler(timezone=TZ)
    now = datetime.now(UTC)

    def collector_job(name):
        return lambda: _run_job(
            conn_factory, name, lambda conn: poll_once(conn, settings, sources_cfg, name))

    for cls in COLLECTOR_CLASSES:
        scheduler.add_job(collector_job(cls.name), IntervalTrigger(
            seconds=cls.interval.total_seconds(), timezone=TZ),
            id=cls.name, name=cls.name, max_instances=1, coalesce=True, next_run_time=now)

    def on_new_candidate(candidate_id: int) -> None:
        def run(conn):
            enqueue_for_candidate(conn, candidate_id)
            conn.commit()
        _run_job(conn_factory, f"enqueue({candidate_id})", run)

    scheduler.add_job(
        lambda: _run_job(conn_factory, "process_raw", lambda conn: process_pending_raw_items(
            conn, llm, embedder, settings.extractor_model, on_new_candidate,
            budget_usd=settings.daily_budget_usd)),
        IntervalTrigger(minutes=1, timezone=TZ),
        id="process_raw", name="process_raw", max_instances=1, coalesce=True)

    def digest(conn):
        since = datetime.now(UTC) - timedelta(hours=24)
        dispatch_digest(conn, settings, render_digest(conn, since, settings.base_url))

    scheduler.add_job(
        lambda: _run_job(conn_factory, "digest", digest),
        CronTrigger(hour=9, minute=0, timezone=TZ),
        id="digest", name="digest", max_instances=1, coalesce=True)

    def trigger_poll(name: str) -> None:
        scheduler.modify_job(name, next_run_time=datetime.now(UTC))

    return scheduler, trigger_poll


def run(settings) -> None:
    """`radar run`: migrate, sync config, then scheduler + tester thread + web server."""
    import uvicorn

    from radar import db
    from radar.llm.openrouter import OpenRouterClient
    from radar.testing.budget import ProviderRateLimiter
    from radar.testing.goals import load_goals, load_models, sync_config
    from radar.testing.tester import tester_loop
    from radar.web.app import create_app

    def conn_factory():
        return db.connect(settings.database_url)

    sources_cfg = load_sources()
    goals = load_goals(CONFIG_DIR / "goals.yaml")
    conn = conn_factory()
    try:
        db.migrate(conn)
        sync_config(conn, goals, load_models(CONFIG_DIR / "models.yaml"))
        conn.commit()
        seed_watchlist(conn, sources_cfg)
    finally:
        conn.close()

    scheduler, trigger_poll = build_scheduler(conn_factory, settings, sources_cfg=sources_cfg)
    stop = threading.Event()
    tester = threading.Thread(
        target=tester_loop, name="tester", daemon=True,
        args=(conn_factory, OpenRouterClient(settings.openrouter_api_key), settings, stop,
              make_on_tier_done(conn_factory, settings), goals, ProviderRateLimiter()))
    scheduler.start()
    tester.start()
    try:
        app = create_app(settings, conn_factory, trigger_poll)
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
    finally:
        stop.set()
        scheduler.shutdown(wait=False)
        tester.join(timeout=10)
