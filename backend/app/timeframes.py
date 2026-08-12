"""Report-day math — the heart of this app.

The owner works US Eastern; some protected servers back up on UK time. A naive
"last night = ET 18:00 to ET 08:00" window silently drops them: a UK job that
runs 22:00 London finishes at 17:00 ET, *before* an Eastern evening window even
opens, and would never appear in the morning review.

So a backup event is not filed by wall-clock time. It is filed by the **backup
night it belongs to**, computed in the *server's own* timezone:

    report_date(event) = local_date_of(end_time_local + (24 - cutoff_hour))

With the default cutoff of 12:00 local, everything that finishes between noon on
day D-1 and noon on day D belongs to report date D — i.e. "the night leading
into the morning of D", in local terms, wherever the server lives.

    UK job ends 23:00 Mon London  (18:00 ET Mon)  -> report date Tue
    UK job ends 02:00 Tue London  (21:00 ET Mon)  -> report date Tue
    US job ends 23:00 Mon ET                       -> report date Tue
    Azure job ends 03:00 Tue ET                    -> report date Tue

All four land on Tuesday, so Tuesday morning's dashboard shows one coherent
"last night" across three continents' worth of schedules.

The cutoff is configurable per server (`night_cutoff_hour`) because it is really
the question "when does this server's backup day start?". A server that backs up
mid-afternoon needs a different divider than one that backs up at midnight.

Everything in the database is stored UTC and naive (SQL Server `datetime2` has no
offset). These helpers are the only place that converts.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Sane fallback if a stored timezone name is unknown (bad hand edit, tzdata gap).
FALLBACK_TZ = "America/New_York"
DEFAULT_CUTOFF_HOUR = 12


@lru_cache(maxsize=64)
def zone(name: str | None) -> ZoneInfo:
    """Resolve an IANA name, never raising — an unknown zone must not 500 the API."""
    try:
        return ZoneInfo(name or FALLBACK_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(FALLBACK_TZ)


def utcnow() -> datetime:
    """Naive UTC — matches how every datetime column is stored."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc(value: datetime) -> datetime:
    """Naive-UTC form of any datetime, aware or not (naive is assumed UTC)."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def to_local(value: datetime, tz_name: str | None) -> datetime:
    """Naive-UTC -> aware local time in `tz_name`."""
    return value.replace(tzinfo=timezone.utc).astimezone(zone(tz_name))


def report_date(end_utc: datetime, tz_name: str | None, cutoff_hour: int = DEFAULT_CUTOFF_HOUR) -> date:
    """The backup night this event belongs to, in the server's own local terms."""
    local = to_local(end_utc, tz_name)
    return (local + timedelta(hours=24 - cutoff_hour)).date()


def report_date_str(
    end_utc: datetime, tz_name: str | None, cutoff_hour: int = DEFAULT_CUTOFF_HOUR
) -> str:
    return report_date(end_utc, tz_name, cutoff_hour).isoformat()


def night_window_utc(
    day: date, tz_name: str | None, cutoff_hour: int = DEFAULT_CUTOFF_HOUR
) -> tuple[datetime, datetime]:
    """The [start, end) UTC span of one server's backup night for `day`.

    Local `day - 1` at the cutoff hour through local `day` at the cutoff hour.
    Returned naive-UTC so it can be compared against stored columns directly.
    """
    tz = zone(tz_name)
    start_local = datetime.combine(day - timedelta(days=1), time(hour=cutoff_hour), tzinfo=tz)
    end_local = datetime.combine(day, time(hour=cutoff_hour), tzinfo=tz)
    return as_utc(start_local), as_utc(end_local)


def current_report_date(tz_name: str, now_utc: datetime | None = None) -> date:
    """The night the dashboard means by "last night", for a viewer in `tz_name`.

    This is simply the viewer's local calendar date. At 09:00 Wednesday that is
    Wednesday — the night of Tue->Wed, which is what someone walking in means. At
    16:00 Wednesday it is *still* Wednesday: Thursday's window has technically
    opened (past the noon cutoff) but almost nothing has run in it yet, and "last
    night" has not changed meaning for a human. It rolls over at local midnight.

    Deliberately NOT `report_date(now)` — that would jump the dashboard forward
    to an empty, not-yet-happened night every day at lunchtime.
    """
    return to_local(now_utc or utcnow(), tz_name).date()


def recent_report_dates(tz_name: str, days: int, now_utc: datetime | None = None) -> list[str]:
    """`days` report dates ending at the current one, oldest first."""
    end = current_report_date(tz_name, now_utc)
    return [(end - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]


def offset_label(tz_name: str | None, at_utc: datetime | None = None) -> str:
    """e.g. "BST (UTC+1)" — so the UI can show *why* a UK row looks time-shifted."""
    local = to_local(at_utc or utcnow(), tz_name)
    abbrev = local.tzname() or ""
    total = int((local.utcoffset() or timedelta()).total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total) // 60, 60)
    stamp = f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")
    return f"{abbrev} ({stamp})" if abbrev else stamp


def fmt_duration(seconds: int | None) -> str:
    """Compact human duration: 45s / 12m / 3h 20m."""
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if secs < 30 else f"{minutes}m {secs}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h" if mins == 0 else f"{hours}h {mins}m"
