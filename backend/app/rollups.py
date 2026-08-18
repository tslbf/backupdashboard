"""Turning raw events into per-night verdicts — including the ones that are
absent.

A failed job is loud: there is a row, with a red result, and any query finds it.
A job that simply never ran is silent — there is nothing to select. That silence
is the most dangerous state a backup estate has, and it is the reason this module
materializes a `ServerDay` row for every (night, server, source) that *should*
have produced a run. Absence becomes `missed`, and `missed` is queryable.

"Should have produced a run" is deliberately conservative: a server counts as
expected on a night only if it was still alive around then (it has some event
within `stale_server_days` of that night) and is flagged `expected`. Otherwise a
decommissioned box generates a fresh red row every night, forever, and the
dashboard trains its reader to ignore red.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from .config import get_settings
from .models import LEGACY_SOURCE, BackupEvent, Server, ServerDay, visible_servers
from .outcomes import MISSED, PROTECTED_OUTCOMES, SUCCESS, severity, worst
from .timeframes import current_report_date

log = logging.getLogger(__name__)

# Sources that only ever backfill history never generate "No backup" rows: the
# legacy table stopped being written the day the collectors took over, and every
# night after that would otherwise read as a miss.
NON_EXPECTING_SOURCES = {LEGACY_SOURCE}

# Sources whose API reports *current state* rather than history.
#
# Cove's EnumerateAccountStatistics returns one row per device carrying its
# latest session — there is no date range and no way to ask what happened on a
# night that has passed. So for these sources an unobserved night and an empty
# night are indistinguishable, and marking every unpolled night "No backup" is
# the dashboard inventing an outage: a weekend with the collector switched off
# came back as a solid band of red across the entire UK estate, every one of
# which had in fact backed up fine.
#
# Veeam and Azure are unaffected — both have real history endpoints, so asking
# for 96 hours and finding a night absent means it genuinely was.
CURRENT_STATE_SOURCES = {"nable"}


def refresh_days(session: Session, dates: list[str]) -> int:
    """Rebuild ServerDay for the given report dates. Returns rows written."""
    if not dates:
        return 0
    settings = get_settings()
    stale_days = settings.stale_server_days

    dates = sorted(set(dates))
    span_start = (date.fromisoformat(dates[0]) - timedelta(days=stale_days)).isoformat()
    span_end = dates[-1]

    servers = {
        s.id: s
        for s in session.query(Server).filter(visible_servers()).all()
    }

    # One pass over the window; everything below is in-memory grouping.
    rows = (
        session.query(
            BackupEvent.server_id,
            BackupEvent.source,
            BackupEvent.report_date,
            BackupEvent.outcome,
            BackupEvent.result_raw,
            BackupEvent.duration_sec,
            BackupEvent.end_utc,
        )
        .filter(BackupEvent.report_date >= span_start, BackupEvent.report_date <= span_end)
        .all()
    )

    # (date, server, source) -> list of events
    by_key: dict[tuple[str, int, str], list] = defaultdict(list)
    # (server, source) -> sorted list of dates it produced anything on
    activity: dict[tuple[int, str], list[str]] = defaultdict(list)
    for row in rows:
        by_key[(row.report_date, row.server_id, row.source)].append(row)
        activity[(row.server_id, row.source)].append(row.report_date)
    for key in activity:
        activity[key].sort()

    target = set(dates)
    session.query(ServerDay).filter(ServerDay.report_date.in_(dates)).delete(
        synchronize_session=False
    )

    written = 0
    for key, events in by_key.items():
        day, server_id, source = key
        if day not in target or server_id not in servers:
            continue
        outcome = worst(e.outcome for e in events)
        # The event that justifies the verdict is the one whose raw result the
        # UI should show — not an arbitrary row from the group.
        lead = min(events, key=lambda e: (severity(e.outcome), -(e.duration_sec or 0)))
        durations = [e.duration_sec for e in events if e.duration_sec is not None]
        ends = [e.end_utc for e in events if e.end_utc is not None]
        session.add(
            ServerDay(
                report_date=day,
                server_id=server_id,
                source=source,
                outcome=outcome,
                event_count=len(events),
                # Longest run of the night. Summing parallel jobs would invent a
                # duration nothing actually took.
                duration_sec=max(durations) if durations else None,
                end_utc=max(ends) if ends else None,
                result_raw=lead.result_raw,
            )
        )
        written += 1

    # --- the absent ones ---------------------------------------------------
    observed = _observed_nights(session, servers, dates, settings)

    for (server_id, source), seen_dates in activity.items():
        server = servers.get(server_id)
        if server is None or not server.expected or source in NON_EXPECTING_SOURCES:
            continue
        for day in dates:
            if (day, server_id, source) in by_key:
                continue
            # A current-state source can only be judged on a night it was
            # actually polled during. Without this, every night the app was off
            # reads as an estate-wide outage — and the morning digest emails it.
            if source in CURRENT_STATE_SOURCES and day not in observed.get(
                (source, server_id), ()
            ):
                continue
            if _was_live(seen_dates, day, stale_days):
                session.add(
                    ServerDay(
                        report_date=day,
                        server_id=server_id,
                        source=source,
                        outcome=MISSED,
                        event_count=0,
                    )
                )
                written += 1

    session.commit()
    return written


def _observed_nights(
    session: Session, servers: dict, dates: list[str], settings
) -> dict[tuple[str, int], set[str]]:
    """Which nights a current-state collector was actually in a position to see.

    Cove tells us one thing: each device's most recent backup, as of the moment
    we asked. So a poll at time T says something about the night T falls in —
    and about the night before it, which had already closed when we looked. It
    says nothing at all about a night three days earlier, whose session has long
    since been overwritten by a later one.

    Hence the window: a run credits its own report date **and the one before**.
    A poll at 08:00 sits inside the night it is reporting on (nights run noon to
    noon, so the overnight job has already finished); a poll at 15:00 has rolled
    into the following night but still saw the one that closed at noon. Stamping
    the run with `report_date` and crediting `{that night, the previous one}`
    covers both without needing to know which case it is.

    Keyed per server because the stamp depends on the server's own timezone: a
    single 08:00 Eastern run lands on a different night for a London machine
    than for a Pittsburgh one, which is the whole premise of this app.
    """
    from .ingest import effective_cutoff, effective_timezone
    from .models import CollectorRun, SourceConfig
    from .timeframes import report_date_str

    if not servers or not CURRENT_STATE_SOURCES:
        return {}

    # Runs a few days either side of the window being rebuilt: a run credits the
    # night before its own, and timezones move the boundary.
    first = date.fromisoformat(dates[0]) - timedelta(days=3)
    last = date.fromisoformat(dates[-1]) + timedelta(days=3)
    runs = (
        session.query(CollectorRun.source, CollectorRun.started_at)
        .filter(
            CollectorRun.source.in_(sorted(CURRENT_STATE_SOURCES)),
            CollectorRun.status == "success",
            CollectorRun.started_at >= datetime.combine(first, time.min),
            CollectorRun.started_at <= datetime.combine(last, time.max),
        )
        .all()
    )
    if not runs:
        return {}

    config = {c.source: c for c in session.query(SourceConfig).all()}
    # Most servers of a source share a timezone, so the stamping is memoized on
    # (source, tz, cutoff) rather than recomputed per server.
    by_zone: dict[tuple[str, str, int], set[str]] = {}
    out: dict[tuple[str, int], set[str]] = {}

    for source in sorted(CURRENT_STATE_SOURCES):
        started_times = [r.started_at for r in runs if r.source == source and r.started_at]
        if not started_times:
            continue
        for server_id, server in servers.items():
            tz = effective_timezone(server, config, settings)
            cutoff = effective_cutoff(server, settings)
            key = (source, tz, cutoff)
            if key not in by_zone:
                nights: set[str] = set()
                for started in started_times:
                    stamp = report_date_str(started, tz, cutoff)
                    nights.add(stamp)
                    # The night that had already closed when we looked.
                    nights.add((date.fromisoformat(stamp) - timedelta(days=1)).isoformat())
                by_zone[key] = nights
            out[(source, server_id)] = by_zone[key]
    return out


def _was_live(seen_dates: list[str], day: str, stale_days: int) -> bool:
    """Did this (server, source) pair look alive around `day`?

    True when it produced a backup within `stale_days` on either side. Looking
    forward as well as back matters for backfill: importing history for a server
    that was fine all along should not paint its first weeks as missed just
    because nothing older exists to prove it was alive.
    """
    target = date.fromisoformat(day)
    window = timedelta(days=stale_days)
    return any(abs(date.fromisoformat(d) - target) <= window for d in seen_dates)


def refresh_recent(session: Session, days: int = 5) -> int:
    """Rebuild the last few nights.

    Run after every collection: results arrive late (a job that started before
    midnight finishes after it), and a night is not final until well past its
    own cutoff.
    """
    settings = get_settings()
    end = current_report_date(settings.display_timezone)
    dates = [(end - timedelta(days=n)).isoformat() for n in range(days)]
    return refresh_days(session, dates)


def refresh_all(session: Session) -> int:
    """Rebuild every night present in the event history (backfill / restamp)."""
    bounds = session.query(
        func.min(BackupEvent.report_date), func.max(BackupEvent.report_date)
    ).one()
    if not bounds[0]:
        return 0
    start = date.fromisoformat(bounds[0])
    # Always run through today even if the newest event is older, so a currently
    # dark estate shows as missed rather than as no data at all.
    end = max(
        date.fromisoformat(bounds[1]),
        current_report_date(get_settings().display_timezone),
    )
    dates = [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]
    total = 0
    # Chunked so a multi-year backfill doesn't build one enormous transaction.
    for i in range(0, len(dates), 60):
        total += refresh_days(session, dates[i : i + 60])
    return total


def refresh_server_summaries(session: Session) -> None:
    """Recompute servers.last_event_utc / last_success_utc."""
    latest = dict(
        session.query(BackupEvent.server_id, func.max(BackupEvent.end_utc))
        .group_by(BackupEvent.server_id)
        .all()
    )
    succeeded = dict(
        session.query(BackupEvent.server_id, func.max(BackupEvent.end_utc))
        .filter(BackupEvent.outcome.in_(PROTECTED_OUTCOMES))
        .group_by(BackupEvent.server_id)
        .all()
    )
    for server in session.query(Server).all():
        server.last_event_utc = latest.get(server.id)
        server.last_success_utc = succeeded.get(server.id)
    session.commit()


def after_collection(session: Session, days: int = 5) -> None:
    """The standard post-collector refresh."""
    refresh_server_summaries(session)
    refresh_days(session, _recent_dates(days))


def _recent_dates(days: int) -> list[str]:
    end = current_report_date(get_settings().display_timezone)
    return [(end - timedelta(days=n)).isoformat() for n in range(days)]


def prune_events(session: Session) -> int:
    """Drop events past the retention window. 0 retention days keeps everything."""
    settings = get_settings()
    if settings.event_retention_days <= 0:
        return 0
    cutoff = (
        current_report_date(settings.display_timezone)
        - timedelta(days=settings.event_retention_days)
    ).isoformat()
    removed = (
        session.query(BackupEvent)
        .filter(BackupEvent.report_date < cutoff)
        .delete(synchronize_session=False)
    )
    session.query(ServerDay).filter(ServerDay.report_date < cutoff).delete(
        synchronize_session=False
    )
    session.commit()
    return removed


def consecutive_failures(session: Session, server_id: int, before: str) -> int:
    """How many nights in a row this server has been failing, counting back from
    `before` inclusive. Drives the "chronic" flag on the failure list — one bad
    night is noise, five is a broken job nobody has looked at."""
    days = (
        session.query(ServerDay)
        .filter(ServerDay.server_id == server_id, ServerDay.report_date <= before)
        .order_by(ServerDay.report_date.desc())
        .limit(60)
        .all()
    )
    by_date: dict[str, list[str]] = defaultdict(list)
    for day in days:
        by_date[day.report_date].append(day.outcome)
    streak = 0
    for day in sorted(by_date, reverse=True):
        if worst(by_date[day]) == SUCCESS:
            break
        streak += 1
    return streak
