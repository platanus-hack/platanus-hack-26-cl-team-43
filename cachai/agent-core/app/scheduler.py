import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.agents.graph import run_audit_cycle
from app.config import get_settings

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _scheduled_job() -> None:
    logger.info("APScheduler triggered periodic audit cycle.")
    try:
        result = run_audit_cycle(trigger_source="scheduler")
        logger.info(
            "Scheduled cycle finished: model=%s approved=%s post=%s",
            (result.get("target_model") or {}).get("model_name"),
            result.get("budget_approved"),
            result.get("published_post_slug"),
        )
    except Exception:
        logger.exception("Scheduled audit cycle failed.")


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        settings = get_settings()
        _scheduler = BackgroundScheduler()
        _scheduler.add_job(
            _scheduled_job,
            trigger="interval",
            minutes=max(1, settings.audit_interval_minutes),
            id="continuous_audit_cycle",
            replace_existing=True,
        )
        _scheduler.start()
        logger.info(
            "APScheduler started (interval=%d min).",
            max(1, settings.audit_interval_minutes),
        )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
