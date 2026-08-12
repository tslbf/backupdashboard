"""A synthetic estate for evaluating the UI without touching production.

Deliberately shaped to exercise the cases that matter rather than to look tidy:
a mostly-green fleet, two chronically broken jobs, a server that has never once
succeeded, jobs that silently stop running, and — the point of the whole
exercise — a block of UK servers whose backups run at a wall-clock time that
falls on the *previous* Eastern day and still have to land in tonight's review.

    python -m app.cli seed-demo
"""
from __future__ import annotations

import random
from datetime import datetime, time, timedelta

from .config import get_settings
from .db import session_factory
from .models import BackupEvent, Server, ServerDay
from .outcomes import FAILED, RUNNING, SUCCESS, UNKNOWN, WARNING
from .timeframes import as_utc, current_report_date, report_date_str, zone

DAYS = 75
SEED = 20260812

# (name, source, timezone, local backup window, personality)
US_TZ = "America/New_York"
UK_TZ = "Europe/London"

VEEAM_SERVERS = [
    "PGHDC01", "PGHDC02", "PGHSQL01", "PGHSQL02", "PGHAPP01", "PGHAPP02",
    "PGHFILE01", "PGHPRINT01", "PGHWEB01", "PGHERP01", "PGHERP02", "PGHRDS01",
    "DUBDC01", "DUBSQL01", "DUBAPP01", "DUBFILE01", "DUBWEB01",
    "BEDFILE01", "BEDAPP01", "HILSQL01", "WILFILE01", "LODAPP01",
]

NABLE_SERVERS = [
    "NOTDC01", "NOTSQL01", "NOTFILE01", "NOTAPP01", "NOTERP01",
    "SHEDC01", "SHEFILE01", "SHEAPP01",
    "LONDC01", "LONSQL01", "LONFILE01", "LONAPP01", "LONWEB01",
    "BILFILE01", "BILAPP01",
]

AZURE_SERVERS = [
    "AZUSCCM01", "AZUSSQL01", "AZUSSQL02", "AZUSAPP01", "AZUSAPP02",
    "AZUSWEB01", "AZUSWEB02", "AZUSDC01", "AZUSFILE01", "AZUSBI01",
    "AZUKAPP01", "AZUKSQL01",
]

# Servers with a story. Everything else is boringly healthy, which is what a
# real estate mostly looks like and what makes the exceptions readable.
CHRONIC_FAILURES = {"PGHERP02", "NOTFILE01"}      # broken for weeks
NEVER_SUCCEEDED = {"AZUKSQL01"}                    # onboarded wrong, never worked
WENT_DARK = {"BEDAPP01"}                           # job silently stopped ~6 nights ago
FLAKY = {"PGHSQL02", "LONSQL01", "AZUSWEB02", "DUBFILE01"}  # intermittent
SLOW = {"PGHERP01", "AZUSBI01", "NOTERP01"}        # long-running, growing


def _local_dt(day, hour: int, minute: int, tz_name: str) -> datetime:
    """Naive-UTC instant for a local wall-clock time on `day`."""
    return as_utc(datetime.combine(day, time(hour=hour, minute=minute), tzinfo=zone(tz_name)))


def seed_demo() -> None:
    from .db import init_db
    from .rollups import refresh_all, refresh_server_summaries

    init_db()
    rng = random.Random(SEED)
    settings = get_settings()
    today = current_report_date(settings.display_timezone)

    roster: list[tuple[str, str, str]] = (
        [(n, "veeam", US_TZ) for n in VEEAM_SERVERS]
        + [(n, "nable", UK_TZ) for n in NABLE_SERVERS]
        + [(n, "azure", US_TZ) for n in AZURE_SERVERS]
    )

    with session_factory()() as session:
        # Idempotent: a re-seed replaces the demo estate rather than doubling it.
        session.query(ServerDay).delete(synchronize_session=False)
        session.query(BackupEvent).delete(synchronize_session=False)
        session.query(Server).delete(synchronize_session=False)
        session.commit()

        servers: dict[str, Server] = {}
        for name, source, _tz in roster:
            server = Server(
                name=name,
                display_name=name,
                primary_source=source,
                # NULL: the server inherits its source's default timezone, which
                # is exactly how a real collector would leave it.
                timezone=None,
                expected=True,
                hidden=False,
            )
            session.add(server)
            servers[name] = server
        session.flush()

        for name, source, tz_name in roster:
            server = servers[name]
            _seed_server(session, rng, server, source, tz_name, today)

        session.commit()
        refresh_server_summaries(session)
        refresh_all(session)


def _seed_server(session, rng: random.Random, server: Server, source: str, tz_name: str, today) -> None:
    # Each server keeps a stable slot in the night so the duration chart has a
    # believable shape instead of uniform noise.
    if source == "azure":
        base_hour, spread = 1, 3
    elif source == "nable":
        base_hour, spread = 22, 5
    else:
        base_hour, spread = 21, 5

    slot = rng.randint(0, spread)
    minute = rng.randrange(0, 60, 5)
    base_minutes = rng.randint(8, 55)
    if server.name in SLOW:
        base_minutes = rng.randint(180, 300)

    for offset in range(DAYS):
        day = today - timedelta(days=DAYS - 1 - offset)
        age = DAYS - 1 - offset  # 0 = last night

        outcome = _pick_outcome(rng, server.name, age)
        if outcome is None:
            continue  # no run at all: the rollup turns this into "No backup"

        hour = (base_hour + slot) % 24
        # Hours past midnight belong to the morning side of the same night.
        start_day = day if hour < 12 else day - timedelta(days=1)
        start_utc = _local_dt(start_day, hour, minute, tz_name)

        # Durations drift upward for the "slow" servers and blow out on failures.
        growth = 1 + (offset / DAYS) * (0.6 if server.name in SLOW else 0.12)
        minutes = base_minutes * growth * rng.uniform(0.85, 1.2)
        if outcome == FAILED:
            minutes *= rng.uniform(0.05, 0.4)  # failures die early
        duration = max(60, int(minutes * 60))
        end_utc = start_utc + timedelta(seconds=duration)

        session.add(
            BackupEvent(
                server_id=server.id,
                source=source,
                job_name=_job_name(source, server.name),
                start_utc=start_utc,
                end_utc=end_utc,
                duration_sec=duration,
                outcome=outcome,
                result_raw=_raw_result(source, outcome),
                report_date=report_date_str(end_utc, tz_name),
                bytes_transferred=int(rng.uniform(4, 900) * 1_073_741_824)
                if outcome != FAILED
                else None,
                details={"demo": True},
            )
        )


def _pick_outcome(rng: random.Random, name: str, age: int) -> str | None:
    """None means the job never ran that night."""
    if name in NEVER_SUCCEEDED:
        return FAILED if rng.random() < 0.85 else None
    if name in WENT_DARK:
        # Ran fine, then simply stopped six nights ago — the silent case.
        return None if age <= 5 else _healthy(rng)
    if name in CHRONIC_FAILURES:
        if age <= 12:
            return FAILED if rng.random() < 0.9 else WARNING
        return _healthy(rng)
    if name in FLAKY:
        roll = rng.random()
        if roll < 0.14:
            return FAILED
        if roll < 0.30:
            return WARNING
        if roll < 0.33:
            return None
        return SUCCESS
    # The healthy majority: still not perfect, because nothing is.
    roll = rng.random()
    if roll < 0.015:
        return FAILED
    if roll < 0.06:
        return WARNING
    if roll < 0.07:
        return None
    if age == 0 and roll > 0.985:
        return RUNNING
    return SUCCESS


def _healthy(rng: random.Random) -> str:
    return WARNING if rng.random() < 0.08 else SUCCESS


def _job_name(source: str, name: str) -> str:
    if source == "veeam":
        return f"{name} Backup"
    if source == "nable":
        return "Cove backup"
    return "AzureIaasVM"


def _raw_result(source: str, outcome: str) -> str:
    if source == "veeam":
        return {SUCCESS: "Success", WARNING: "Warning", FAILED: "Failed", RUNNING: "None"}.get(
            outcome, "Unknown"
        )
    if source == "nable":
        return {
            SUCCESS: "Completed",
            WARNING: "CompletedWithErrors",
            FAILED: "Failed",
            RUNNING: "InProcess",
        }.get(outcome, "Unknown")
    return {
        SUCCESS: "Completed",
        WARNING: "CompletedWithWarnings",
        FAILED: "Failed",
        RUNNING: "InProgress",
    }.get(outcome, UNKNOWN)
