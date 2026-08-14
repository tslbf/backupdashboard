from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .collectors import ALL_COLLECTORS, run_collector
from .config import get_settings
from .db import get_session
from .ingest import effective_cutoff, effective_timezone, restamp_server, timezone_origin
from .models import (
    BackupEvent,
    CollectorRun,
    Server,
    ServerDay,
    SourceConfig,
    expected_servers,
    visible_servers,
)
from .outcomes import (
    ALL_OUTCOMES,
    FAILED,
    MISSED,
    OUTCOME_LABELS,
    PROBLEM_OUTCOMES,
    PROTECTED_OUTCOMES,
    RUNNING,
    SUCCESS,
    UNKNOWN,
    WARNING,
    severity,
    worst,
)
from .timeframes import (
    current_report_date,
    fmt_duration,
    offset_label,
    report_date,
    to_local,
    utcnow,
)

router = APIRouter(prefix="/api")

STREAK_LOOKBACK = 60
HEATMAP_DAYS = 14


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _source_config(session: Session) -> dict[str, SourceConfig]:
    return {c.source: c for c in session.query(SourceConfig).all()}


def _display_name(config: dict[str, SourceConfig], source: str) -> str:
    cfg = config.get(source)
    return cfg.display_name if cfg else source.title()


def _resolve_date(value: str | None) -> str:
    if value:
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    return current_report_date(get_settings().display_timezone).isoformat()


def _empty_counts() -> dict[str, int]:
    return {outcome: 0 for outcome in ALL_OUTCOMES}


def _server_payload(server: Server, config: dict[str, SourceConfig], settings) -> dict:
    tz = effective_timezone(server, config, settings)
    return {
        "id": server.id,
        "name": server.name,
        "display_name": server.display_name or server.name,
        "primary_source": server.primary_source,
        "timezone": tz,
        "timezone_origin": timezone_origin(server),
        "timezone_label": offset_label(tz),
        "night_cutoff_hour": effective_cutoff(server, settings),
        "cutoff_is_override": server.night_cutoff_hour is not None,
        "expected": server.expected,
        "hidden": server.hidden,
        "notes": server.notes,
        "last_event_utc": _iso(server.last_event_utc),
        "last_success_utc": _iso(server.last_success_utc),
    }


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _local_time(value, tz: str) -> str | None:
    """HH:MM in the server's own timezone — the number that explains why a UK
    job sitting at 17:00 Eastern belongs to tonight."""
    if value is None:
        return None
    return to_local(value, tz).strftime("%H:%M")


def _streaks(session: Session, server_ids: list[int], before: str) -> dict[int, int]:
    """Consecutive non-clean nights per server, counted back from `before`.

    One bad night is weather; five in a row is a job nobody has looked at, and
    that distinction is what makes the failure list actionable.
    """
    if not server_ids:
        return {}
    start = (date.fromisoformat(before) - timedelta(days=STREAK_LOOKBACK)).isoformat()
    rows = (
        session.query(ServerDay.server_id, ServerDay.report_date, ServerDay.outcome)
        .filter(
            ServerDay.server_id.in_(server_ids),
            ServerDay.report_date <= before,
            ServerDay.report_date >= start,
        )
        .all()
    )
    by_server: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for server_id, day, outcome in rows:
        by_server[server_id][day].append(outcome)

    out: dict[int, int] = {}
    for server_id, days in by_server.items():
        streak = 0
        for day in sorted(days, reverse=True):
            if worst(days[day]) == SUCCESS:
                break
            streak += 1
        out[server_id] = streak
    return out


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


SUMMARY_VERSION = 1


@router.get("/summary")
def summary(session: Session = Depends(get_session)):
    """One small, stable payload for another dashboard to embed.

    This is the roll-up surface: the asset dashboard shows a backup tile without
    knowing anything about report dates, sources, or timezones. Everything a
    caller needs to render is precomputed and pre-formatted here, because the
    consumer is a different codebase that must not have to reimplement the
    night model to draw one number.

    The contract is versioned and additive-only. Fields may be added; renaming
    or removing one breaks a caller that this repo cannot see or test, so that
    is a `version` bump and a note in docs/integration.md.
    """
    settings = get_settings()
    data = overview(date_param=None, session=session)
    counts = data["counts"]
    problems = data["problems"]

    worst = problems[0]["outcome"] if problems else (SUCCESS if data["jobs_total"] else "unknown")
    return {
        "version": SUMMARY_VERSION,
        "app": "backup",
        "title": "Backup status",
        "report_date": data["report_date"],
        "generated_at": data["generated_at"],
        "display_timezone": settings.display_timezone,
        # The headline, already worded — so two dashboards cannot phrase the
        # same fact differently.
        "headline": f"{data['servers_protected']} of {data['servers_total']} protected",
        "servers_total": data["servers_total"],
        "servers_protected": data["servers_protected"],
        "protected_pct": data["protected_pct"],
        "needs_attention": len(problems),
        "worst_outcome": worst,
        "counts": counts,
        # A short list, not the whole estate: a tile has room for a few rows and
        # a caller that wants everything should link through instead.
        "problems": [
            {
                "server": p["server"],
                "outcome": p["outcome"],
                "source": p["source_name"],
                "detail": p["result_raw"] or "no run recorded",
                "streak": p["streak"],
            }
            for p in problems[:5]
        ],
        "collectors": [
            {
                "source": c.source,
                "display_name": c.display_name,
                "configured": c.is_configured(settings),
                "status": _last_run_status(session, c.source),
            }
            for c in ALL_COLLECTORS.values()
        ],
    }


def _last_run_status(session: Session, source: str) -> str | None:
    """Stale data is worse than no data on a tile someone else owns — a caller
    needs to be able to say "this number is from a collector that failed"."""
    last = (
        session.query(CollectorRun)
        .filter(CollectorRun.source == source)
        .order_by(CollectorRun.started_at.desc())
        .first()
    )
    return last.status if last else None


@router.get("/meta")
def meta(session: Session = Depends(get_session)):
    """Everything the UI needs to explain the clock to the reader."""
    settings = get_settings()
    config = _source_config(session)
    today = current_report_date(settings.display_timezone)
    return {
        "display_timezone": settings.display_timezone,
        "display_timezone_label": offset_label(settings.display_timezone),
        "night_cutoff_hour": settings.night_cutoff_hour,
        "current_report_date": today.isoformat(),
        "generated_at": _iso(utcnow()),
        "stale_server_days": settings.stale_server_days,
        "sources": [
            {
                "source": cfg.source,
                "display_name": cfg.display_name,
                "default_timezone": cfg.default_timezone,
                "timezone_label": offset_label(cfg.default_timezone),
                "expected": cfg.expected,
            }
            for cfg in sorted(config.values(), key=lambda c: c.sort_order)
        ],
        "outcome_labels": OUTCOME_LABELS,
    }


# ---------------------------------------------------------------------------
# overview — "last night"
# ---------------------------------------------------------------------------


@router.get("/overview")
def overview(
    date_param: str | None = Query(default=None, alias="date"),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    config = _source_config(session)
    day = _resolve_date(date_param)
    previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()

    rows = (
        session.query(ServerDay, Server)
        .join(Server, Server.id == ServerDay.server_id)
        .filter(ServerDay.report_date.in_([day, previous]), visible_servers())
        .all()
    )

    tonight = [(sd, srv) for sd, srv in rows if sd.report_date == day]
    last_night = [(sd, srv) for sd, srv in rows if sd.report_date == previous]

    counts = _empty_counts()
    per_source: dict[str, dict[str, int]] = defaultdict(_empty_counts)
    by_server: dict[int, list[str]] = defaultdict(list)
    for server_day, server in tonight:
        counts[server_day.outcome] = counts.get(server_day.outcome, 0) + 1
        per_source[server_day.source][server_day.outcome] += 1
        by_server[server.id].append(server_day.outcome)

    previous_counts = _empty_counts()
    for server_day, _ in last_night:
        previous_counts[server_day.outcome] = previous_counts.get(server_day.outcome, 0) + 1

    # Server-level verdicts: a machine backed up by two tools is one machine.
    server_verdicts = {sid: worst(outcomes) for sid, outcomes in by_server.items()}
    servers_total = len(server_verdicts)
    servers_protected = sum(1 for v in server_verdicts.values() if v in PROTECTED_OUTCOMES)

    problem_rows = [
        (sd, srv) for sd, srv in tonight if sd.outcome in PROBLEM_OUTCOMES
    ]
    problem_rows.sort(
        key=lambda pair: (severity(pair[0].outcome), pair[1].name)
    )
    streaks = _streaks(session, [srv.id for _, srv in problem_rows], day)

    def night_row(server_day, server) -> dict:
        tz = effective_timezone(server, config, settings)
        return {
            "server_id": server.id,
            "server": server.name,
            "source": server_day.source,
            "source_name": _display_name(config, server_day.source),
            "outcome": server_day.outcome,
            "result_raw": server_day.result_raw,
            "end_utc": _iso(server_day.end_utc),
            "end_local": _local_time(server_day.end_utc, tz),
            "timezone": tz,
            "timezone_label": offset_label(tz),
            "duration_sec": server_day.duration_sec,
            "duration_label": fmt_duration(server_day.duration_sec),
            # Only ever computed for the problem rows: a streak of successes is
            # not a thing anyone chases, and it would cost a query per server.
            "streak": streaks.get(server.id, 1),
            "last_success_utc": _iso(server.last_success_utc),
            "last_success_days": _days_since(server.last_success_utc),
            "event_count": server_day.event_count,
        }

    problems = [night_row(sd, srv) for sd, srv in problem_rows]

    # Every result for the night, worst first. The landing page lists the whole
    # estate with the problems at the top, because "what else ran" is a fair
    # question once you have dealt with the exceptions — and a page that only
    # ever shows failures gives no way to confirm a server you were worried
    # about is fine.
    #
    # `problems` stays the actionable subset, deliberately: the morning digest,
    # the /api/summary tile and `needs_attention` all count it, and widening it
    # would quietly turn "3 need attention" into "56 need attention".
    rows = [
        night_row(sd, srv)
        for sd, srv in sorted(tonight, key=lambda pair: (severity(pair[0].outcome), pair[1].name))
    ]

    return {
        "report_date": day,
        "previous_date": previous,
        "generated_at": _iso(utcnow()),
        "display_timezone": settings.display_timezone,
        "night_cutoff_hour": settings.night_cutoff_hour,
        "is_current": day == current_report_date(settings.display_timezone).isoformat(),
        "counts": counts,
        "previous_counts": previous_counts,
        "servers_total": servers_total,
        "servers_protected": servers_protected,
        "protected_pct": round(servers_protected / servers_total * 100, 1)
        if servers_total
        else None,
        "jobs_total": sum(counts.values()),
        "problems": problems,
        "rows": rows,
        "per_source": [
            {
                "source": source,
                "display_name": _display_name(config, source),
                "default_timezone": config[source].default_timezone if source in config else None,
                "timezone_label": offset_label(
                    config[source].default_timezone if source in config else None
                ),
                **values,
                "total": sum(values.values()),
            }
            for source, values in sorted(
                per_source.items(),
                key=lambda item: config[item[0]].sort_order if item[0] in config else 99,
            )
        ],
        "attention": _attention(session, config, settings, day),
    }


def _days_since(value) -> int | None:
    if value is None:
        return None
    return max(0, (utcnow() - value).days)


def _attention(session: Session, config, settings, day: str) -> dict:
    """Two slower-burning risks the nightly counts don't surface on their own."""
    horizon = utcnow() - timedelta(days=settings.stale_server_days)

    never = (
        session.query(Server)
        .filter(
            visible_servers(),
            expected_servers(),
            Server.last_success_utc.is_(None),
            Server.last_event_utc.isnot(None),
            Server.last_event_utc >= horizon,
        )
        .order_by(Server.name)
        .limit(50)
        .all()
    )

    # Backed up at some point, but not successfully in a long time — these never
    # appear as tonight's failure once the job stops running entirely.
    stale_cutoff = utcnow() - timedelta(days=3)
    stale = (
        session.query(Server)
        .filter(
            visible_servers(),
            expected_servers(),
            Server.last_success_utc.isnot(None),
            Server.last_success_utc < stale_cutoff,
        )
        .order_by(Server.last_success_utc)
        .limit(50)
        .all()
    )

    return {
        "never_succeeded": [
            {
                "server_id": s.id,
                "server": s.name,
                "last_event_utc": _iso(s.last_event_utc),
            }
            for s in never
        ],
        "stale_success": [
            {
                "server_id": s.id,
                "server": s.name,
                "last_success_utc": _iso(s.last_success_utc),
                "days": _days_since(s.last_success_utc),
            }
            for s in stale
        ],
    }


# ---------------------------------------------------------------------------
# trends
# ---------------------------------------------------------------------------


@router.get("/trends")
def trends(
    days: int = Query(default=30, ge=2, le=400),
    source: str | None = None,
    session: Session = Depends(get_session),
):
    """Daily outcome counts, one row per report date — the history chart."""
    settings = get_settings()
    end = current_report_date(settings.display_timezone)
    start = (end - timedelta(days=days - 1)).isoformat()

    query = (
        session.query(ServerDay.report_date, ServerDay.outcome, func.count(ServerDay.id))
        .join(Server, Server.id == ServerDay.server_id)
        .filter(
            visible_servers(),
            ServerDay.report_date >= start,
            ServerDay.report_date <= end.isoformat(),
        )
    )
    if source:
        query = query.filter(ServerDay.source == source)
    rows = query.group_by(ServerDay.report_date, ServerDay.outcome).all()

    by_date: dict[str, dict[str, int]] = defaultdict(_empty_counts)
    for day, outcome, count in rows:
        by_date[day][outcome] = count

    series = []
    for offset in range(days):
        day = (end - timedelta(days=days - 1 - offset)).isoformat()
        counts = by_date.get(day, _empty_counts())
        total = sum(counts.values())
        protected = sum(counts[o] for o in PROTECTED_OUTCOMES)
        series.append(
            {
                "date": day,
                **counts,
                "total": total,
                "success_rate": round(protected / total * 100, 1) if total else None,
            }
        )
    return series


@router.get("/trends/duration")
def duration_trend(
    days: int = Query(default=30, ge=2, le=400),
    session: Session = Depends(get_session),
):
    """Nightly backup-window shape: total time and the slowest single job.

    Deliberately two aggregates of the *same* measure on one scale rather than a
    second axis — a duration chart with a count axis bolted on invents a
    correlation that isn't in the data.
    """
    settings = get_settings()
    end = current_report_date(settings.display_timezone)
    start = (end - timedelta(days=days - 1)).isoformat()

    rows = (
        session.query(ServerDay.report_date, ServerDay.duration_sec)
        .join(Server, Server.id == ServerDay.server_id)
        .filter(
            visible_servers(),
            ServerDay.duration_sec.isnot(None),
            ServerDay.report_date >= start,
            ServerDay.report_date <= end.isoformat(),
        )
        .all()
    )
    by_date: dict[str, list[int]] = defaultdict(list)
    for day, duration in rows:
        by_date[day].append(duration)

    series = []
    for offset in range(days):
        day = (end - timedelta(days=days - 1 - offset)).isoformat()
        values = sorted(by_date.get(day, []))
        if not values:
            series.append({"date": day, "median_sec": None, "max_sec": None, "jobs": 0})
            continue
        middle = values[len(values) // 2]
        series.append(
            {
                "date": day,
                "median_sec": middle,
                "max_sec": values[-1],
                "jobs": len(values),
            }
        )
    return series


# ---------------------------------------------------------------------------
# servers
# ---------------------------------------------------------------------------


@router.get("/servers")
def list_servers(
    days: int = Query(default=HEATMAP_DAYS, ge=1, le=90),
    include_hidden: bool = False,
    session: Session = Depends(get_session),
):
    """One row per server with its last `days` nights — the coverage grid."""
    settings = get_settings()
    config = _source_config(session)
    end = current_report_date(settings.display_timezone)
    dates = [(end - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]

    query = session.query(Server)
    if not include_hidden:
        query = query.filter(visible_servers())
    servers = query.order_by(Server.name).all()

    rows = (
        session.query(ServerDay)
        .filter(ServerDay.report_date >= dates[0], ServerDay.report_date <= dates[-1])
        .all()
    )
    history: dict[int, dict[str, list[ServerDay]]] = defaultdict(lambda: defaultdict(list))
    sources_seen: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        history[row.server_id][row.report_date].append(row)
        sources_seen[row.server_id].add(row.source)

    payload = []
    for server in servers:
        server_days = history.get(server.id, {})
        strip = []
        for day in dates:
            entries = server_days.get(day)
            if not entries:
                strip.append({"date": day, "outcome": None})
                continue
            strip.append(
                {
                    "date": day,
                    "outcome": worst(e.outcome for e in entries),
                    "duration_sec": max(
                        (e.duration_sec for e in entries if e.duration_sec is not None),
                        default=None,
                    ),
                    "sources": sorted({e.source for e in entries}),
                }
            )
        recent = [cell["outcome"] for cell in strip if cell["outcome"]]
        payload.append(
            {
                **_server_payload(server, config, settings),
                "sources": sorted(sources_seen.get(server.id, set())),
                "days": strip,
                "success_rate": round(
                    sum(1 for o in recent if o in PROTECTED_OUTCOMES) / len(recent) * 100, 1
                )
                if recent
                else None,
                "problem_nights": sum(1 for o in recent if o in PROBLEM_OUTCOMES),
                "last_outcome": strip[-1]["outcome"] if strip else None,
            }
        )
    return {"dates": dates, "servers": payload}


@router.get("/servers/{server_id}")
def server_detail(
    server_id: int,
    days: int = Query(default=60, ge=7, le=400),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    config = _source_config(session)
    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")

    tz = effective_timezone(server, config, settings)
    end = current_report_date(settings.display_timezone)
    start = (end - timedelta(days=days - 1)).isoformat()

    day_rows = (
        session.query(ServerDay)
        .filter(
            ServerDay.server_id == server_id,
            ServerDay.report_date >= start,
        )
        .order_by(ServerDay.report_date)
        .all()
    )
    events = (
        session.query(BackupEvent)
        .filter(BackupEvent.server_id == server_id)
        .order_by(BackupEvent.end_utc.desc())
        .limit(200)
        .all()
    )

    by_date: dict[str, list[ServerDay]] = defaultdict(list)
    for row in day_rows:
        by_date[row.report_date].append(row)

    timeline = []
    for offset in range(days):
        day = (end - timedelta(days=days - 1 - offset)).isoformat()
        entries = by_date.get(day, [])
        timeline.append(
            {
                "date": day,
                "outcome": worst(e.outcome for e in entries) if entries else None,
                "duration_sec": max(
                    (e.duration_sec for e in entries if e.duration_sec is not None), default=None
                ),
                "sources": sorted({e.source for e in entries}),
            }
        )

    counts = _empty_counts()
    for entries in by_date.values():
        counts[worst(e.outcome for e in entries)] += 1

    return {
        **_server_payload(server, config, settings),
        "counts": counts,
        "timeline": timeline,
        "streak": _streaks(session, [server_id], end.isoformat()).get(server_id, 0),
        "events": [
            {
                "id": e.id,
                "source": e.source,
                "source_name": _display_name(config, e.source),
                "job_name": e.job_name,
                "report_date": e.report_date,
                "start_utc": _iso(e.start_utc),
                "end_utc": _iso(e.end_utc),
                "start_local": _local_time(e.start_utc, tz),
                "end_local": _local_time(e.end_utc, tz),
                "duration_sec": e.duration_sec,
                "duration_label": fmt_duration(e.duration_sec),
                "outcome": e.outcome,
                "result_raw": e.result_raw,
            }
            for e in events
        ],
    }


class ServerUpdate(BaseModel):
    timezone: str | None = None
    night_cutoff_hour: int | None = None
    expected: bool | None = None
    hidden: bool | None = None
    notes: str | None = None
    # Explicit clears, since None already means "leave alone".
    clear_timezone: bool = False
    clear_cutoff: bool = False


@router.patch("/servers/{server_id}")
def update_server(
    server_id: int,
    payload: ServerUpdate,
    session: Session = Depends(get_session),
):
    """Edit a server's timezone/expectation.

    Changing the timezone or cutoff rewrites that server's report-date stamps and
    rebuilds its nights: the stamps are denormalized, so without the restamp the
    heatmap and the event list would disagree about which night a job ran on.
    """
    from .rollups import refresh_all, refresh_server_summaries

    server = session.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")

    if payload.timezone is not None:
        from .timeframes import zone

        if zone(payload.timezone).key != payload.timezone:
            raise HTTPException(status_code=400, detail=f"unknown timezone: {payload.timezone}")

    if payload.night_cutoff_hour is not None and not 0 <= payload.night_cutoff_hour <= 23:
        raise HTTPException(status_code=400, detail="night_cutoff_hour must be 0-23")

    time_changed = False
    if payload.clear_timezone:
        server.timezone = None
        time_changed = True
    elif payload.timezone is not None:
        server.timezone = payload.timezone
        time_changed = True

    if payload.clear_cutoff:
        server.night_cutoff_hour = None
        time_changed = True
    elif payload.night_cutoff_hour is not None:
        server.night_cutoff_hour = payload.night_cutoff_hour
        time_changed = True

    if payload.expected is not None:
        server.expected = payload.expected
    if payload.hidden is not None:
        server.hidden = payload.hidden
    if payload.notes is not None:
        server.notes = payload.notes
    session.commit()

    restamped = 0
    if time_changed:
        restamped = restamp_server(session, server)
        session.commit()
    if time_changed or payload.expected is not None or payload.hidden is not None:
        refresh_server_summaries(session)
        refresh_all(session)

    config = _source_config(session)
    return {**_server_payload(server, config, get_settings()), "events_restamped": restamped}


class BulkServerUpdate(BaseModel):
    server_ids: list[int]
    hidden: bool | None = None
    expected: bool | None = None


@router.post("/servers/bulk")
def bulk_update_servers(
    payload: BulkServerUpdate,
    session: Session = Depends(get_session),
):
    """Hide or unhide several servers at once.

    Separate from `PATCH /servers/{id}` for one reason that matters: both
    `hidden` and `expected` feed missed-detection, so every change has to be
    followed by a rollup rebuild. Doing that per server would rebuild the whole
    estate's nights once per checkbox — for a 40-server selection, forty times.
    This applies them all, then rebuilds once.
    """
    from .rollups import refresh_all, refresh_server_summaries

    if payload.hidden is None and payload.expected is None:
        raise HTTPException(status_code=400, detail="nothing to change")
    if not payload.server_ids:
        raise HTTPException(status_code=400, detail="no servers selected")

    # Chunked: SQL Server caps a statement at 2100 parameters, and "select all"
    # on a large estate is exactly how that gets hit.
    ids = list(dict.fromkeys(payload.server_ids))
    servers: list[Server] = []
    for start in range(0, len(ids), 500):
        servers.extend(
            session.query(Server).filter(Server.id.in_(ids[start : start + 500])).all()
        )
    if not servers:
        raise HTTPException(status_code=404, detail="no such servers")

    for server in servers:
        if payload.hidden is not None:
            server.hidden = payload.hidden
        if payload.expected is not None:
            server.expected = payload.expected
    session.commit()

    refresh_server_summaries(session)
    refresh_all(session)
    return {"updated": len(servers), "ids": [s.id for s in servers]}


@router.get("/timezones")
def timezones():
    """A short, curated list — the estate is US/UK, not the full 600-zone tzdata."""
    return [
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Los_Angeles",
        "America/Vancouver",
        "Europe/London",
        "Europe/Dublin",
        "Europe/Paris",
        "UTC",
    ]


# ---------------------------------------------------------------------------
# one night, drilled down
# ---------------------------------------------------------------------------


@router.get("/days/{day}")
def day_detail(day: str, session: Session = Depends(get_session)):
    settings = get_settings()
    config = _source_config(session)
    day = _resolve_date(day)

    rows = (
        session.query(ServerDay, Server)
        .join(Server, Server.id == ServerDay.server_id)
        .filter(ServerDay.report_date == day, visible_servers())
        .all()
    )
    payload = []
    for server_day, server in sorted(
        rows, key=lambda pair: (severity(pair[0].outcome), pair[1].name)
    ):
        tz = effective_timezone(server, config, settings)
        payload.append(
            {
                "server_id": server.id,
                "server": server.name,
                "source": server_day.source,
                "source_name": _display_name(config, server_day.source),
                "outcome": server_day.outcome,
                "result_raw": server_day.result_raw,
                "end_utc": _iso(server_day.end_utc),
                "end_local": _local_time(server_day.end_utc, tz),
                "timezone": tz,
                "duration_sec": server_day.duration_sec,
                "duration_label": fmt_duration(server_day.duration_sec),
                "event_count": server_day.event_count,
            }
        )
    counts = _empty_counts()
    for row in payload:
        counts[row["outcome"]] += 1
    return {"report_date": day, "counts": counts, "rows": payload}


# ---------------------------------------------------------------------------
# collectors
# ---------------------------------------------------------------------------


def _schedule_label(collector, settings) -> str:
    """What this source's schedule is, in words, for the Collectors page.

    Built here rather than in the UI so there is one description of the
    schedule and it cannot drift from what the scheduler actually does.
    """
    parts: list[str] = []
    daily = settings.collect_time_parts()
    if daily is not None and collector.schedulable(settings):
        zone_label = offset_label(settings.display_timezone) or settings.display_timezone
        parts.append(f"daily at {daily[0]:02d}:{daily[1]:02d} {zone_label}")
    minutes = collector.interval_minutes(settings)
    if minutes > 0:
        parts.append(f"every {minutes} min")
    return " + ".join(parts) if parts else "manual only"


@router.get("/collectors")
def collectors(session: Session = Depends(get_session)):
    settings = get_settings()
    config = _source_config(session)
    out = []
    for collector in ALL_COLLECTORS.values():
        last = (
            session.query(CollectorRun)
            .filter(CollectorRun.source == collector.source)
            .order_by(CollectorRun.started_at.desc())
            .first()
        )
        cfg = config.get(collector.source)
        out.append(
            {
                "source": collector.source,
                "display_name": collector.display_name,
                "configured": collector.is_configured(settings),
                "interval_minutes": collector.interval_minutes(settings),
                "schedule": _schedule_label(collector, settings),
                "default_timezone": cfg.default_timezone if cfg else None,
                "last_run": {
                    "started_at": _iso(last.started_at),
                    "finished_at": _iso(last.finished_at),
                    "status": last.status,
                    "records": last.records,
                    "message": last.message,
                }
                if last
                else None,
            }
        )
    return out


@router.get("/collectors/runs")
def collector_runs(
    limit: int = Query(default=40, ge=1, le=200),
    source: str | None = None,
    session: Session = Depends(get_session),
):
    """Recent runs across all sources — the durable record behind the live log."""
    config = _source_config(session)
    query = session.query(CollectorRun)
    if source:
        query = query.filter(CollectorRun.source == source)
    runs = query.order_by(CollectorRun.started_at.desc()).limit(limit).all()
    return [
        {
            "id": run.id,
            "source": run.source,
            "display_name": _display_name(config, run.source),
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
            "status": run.status,
            "records": run.records,
            "message": run.message,
            "duration_sec": int((run.finished_at - run.started_at).total_seconds())
            if run.finished_at and run.started_at
            else None,
        }
        for run in runs
    ]


@router.get("/logs")
def logs(
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=600),
):
    """Tail of the app log, for the live panel on the Collectors page.

    `after` is the last id the client already has, so polling costs one small
    response rather than the whole buffer each time.
    """
    from .logbuffer import get_handler

    handler = get_handler()
    entries = handler.entries(after=after, limit=limit)
    return {
        "entries": entries,
        "last_id": entries[-1]["id"] if entries else after,
        "buffer_end": handler.last_id(),
        "server_time": _iso(utcnow()),
    }


@router.post("/collectors/{source}/run")
def trigger_collector(source: str, background: BackgroundTasks):
    collector = ALL_COLLECTORS.get(source)
    if collector is None:
        raise HTTPException(status_code=404, detail="unknown source")
    background.add_task(run_collector, collector)
    return {"queued": source}


@router.post("/refresh")
def refresh(background: BackgroundTasks):
    """Rebuild every night from the stored events (after a bulk import)."""
    from .db import session_factory
    from .rollups import refresh_all, refresh_server_summaries

    def _job() -> None:
        with session_factory()() as session:
            refresh_server_summaries(session)
            refresh_all(session)

    background.add_task(_job)
    return {"queued": "refresh"}
