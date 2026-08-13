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


def _morning_run(collectors: list) -> None:
    """Collect, rebuild, then send — in that order, once a morning.

    The ordering is the point. A digest built while Azure is still paging
    reports a night that is half-collected, and every server whose result had
    not arrived yet reads as "No backup" — the app crying wolf at 8am, in an
    email, which is the worst possible place for it.

    `run_collector` already contains its own failures, so one source being down
    still lets the others report and the digest still goes out; it just says so.
    """
    for collector in collectors:
        run_collector(collector)

    _nightly_job()

    from .notify import send_morning_digest

    send_morning_digest()


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled:
        log.info("scheduler disabled via settings")
        return None

    scheduler = BackgroundScheduler(timezone="UTC")
    daily = settings.collect_time_parts()
    if daily is None and (settings.collect_time or "").strip():
        log.warning(
            "COLLECT_TIME=%r is not a valid HH:MM — no daily collection is scheduled",
            settings.collect_time,
        )

    morning: list = []
    for collector in ALL_COLLECTORS.values():
        if not collector.is_configured(settings):
            log.info("collector %s not configured — skipping schedule", collector.source)
            continue
        if not collector.schedulable(settings):
            log.info("collector %s is a backfill source — manual runs only", collector.source)
            continue

        if daily is not None:
            morning.append(collector)

        minutes = collector.interval_minutes(settings)
        if minutes > 0:
            scheduler.add_job(
                run_collector,
                "interval",
                minutes=minutes,
                args=[collector],
                id=f"collect_{collector.source}",
                max_instances=1,
                coalesce=True,
            )
            log.info("also polling %s every %s minutes", collector.source, minutes)
        elif daily is None:
            log.info("collector %s has no schedule — manual runs only", collector.source)

    if morning:
        # One job that runs the collectors in sequence, then rebuilds, then
        # emails — rather than a cron entry per source. The digest has to report
        # on data that has finished arriving, and three independent 08:00 jobs
        # give no way to know when that is.
        #
        # In the viewer's timezone, not UTC: "before I get in" is a local idea,
        # and it has to stay 8am through both DST changes.
        scheduler.add_job(
            _morning_run,
            "cron",
            hour=daily[0],
            minute=daily[1],
            timezone=settings.display_timezone,
            args=[morning],
            id="morning_run",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        log.info(
            "scheduled the morning run at %02d:%02d %s — %s, then rollups%s",
            daily[0],
            daily[1],
            settings.display_timezone,
            ", ".join(c.source for c in morning),
            ", then the digest" if settings.notify_configured() else "",
        )
    elif settings.notify_configured():
        log.warning("SMTP is configured but COLLECT_TIME is not — no digest will be sent")

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
