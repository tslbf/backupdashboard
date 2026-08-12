from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .collectors import ALL_COLLECTORS, run_collector
from .config import get_settings
from .db import session_factory
from .rollups import prune_events, refresh_recent, refresh_server_summaries

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def _nightly_job() -> None:
    """Rebuild recent nights and trim history.

    Rebuilding matters even when no collector ran: "No backup" rows only exist
    because something computes them, so an estate that has gone completely dark
    still has to produce a screen full of red rather than an empty page.
    """
    with session_factory()() as session:
        refresh_server_summaries(session)
        refresh_recent(session, days=7)
        prune_events(session)


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled:
        log.info("scheduler disabled via settings")
        return None

    scheduler = BackgroundScheduler(timezone="UTC")
    for collector in ALL_COLLECTORS.values():
        if not collector.is_configured(settings):
            log.info("collector %s not configured — skipping schedule", collector.source)
            continue
        minutes = collector.interval_minutes(settings)
        if minutes <= 0:
            log.info("collector %s has interval 0 — manual runs only", collector.source)
            continue
        scheduler.add_job(
            run_collector,
            "interval",
            minutes=minutes,
            args=[collector],
            id=f"collect_{collector.source}",
            max_instances=1,
            coalesce=True,
        )
        log.info("scheduled %s every %s minutes", collector.source, minutes)

    # Hourly, not daily: report dates roll over at different wall-clock times for
    # UK and US servers, and a missed night should appear within the hour rather
    # than at some fixed moment that is mid-evening for half the estate.
    scheduler.add_job(_nightly_job, "cron", minute=20, id="rollup_refresh")
    scheduler.start()
    _scheduler = scheduler
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
