"""Server identity, timezone resolution, and event upsert.

Every collector funnels through here so that name normalization and report-day
stamping happen exactly once, in one place, the same way for all three vendors.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .models import Server, SourceConfig
from .timeframes import DEFAULT_CUTOFF_HOUR, report_date_str, utcnow

log = logging.getLogger(__name__)

# Vendors hand back the same machine as "SQL01", "sql01.lbfosterco.com", and
# occasionally "SQL01 (Backup)". All three are one server.
_TRAILING_NOISE = re.compile(r"\s*[\(\[].*?[\)\]]\s*$")


def normalize_name(raw: str | None) -> str | None:
    """Short, uppercase, domain-stripped — the identity key across all sources."""
    if not raw:
        return None
    name = _TRAILING_NOISE.sub("", str(raw).strip())
    if not name:
        return None
    # Domain-strip only when the first label is a plausible hostname; this keeps
    # names that are genuinely dotted (rare, but an Azure friendly name can be)
    # from being truncated to their first word.
    head = name.split(".")[0]
    if head and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", head):
        name = head
    return name.upper()[:128] or None


def to_second(value: datetime | None) -> datetime | None:
    """Drop the sub-second part of a timestamp.

    A backup run is identified by (source, server, start time), and that key is
    an equality test on a datetime column — which makes the column's precision
    part of the key, and the two databases disagree about it.

    SQL Server's DATETIME keeps 1/300 of a second and *rounds* what it is given:
    hand it 20:10:38.239838 and 20:10:38.240 is what comes back. So the value
    searched for is never the value stored, the row is never found, every
    re-collection tries to INSERT the same run again, and the unique constraint
    rejects it:

        Violation of UNIQUE KEY constraint 'uq_event_source_server_start'.
        The duplicate key value is (azure, 55, 2026-08-12 20:10:38.240)

    SQLite stores exactly what it is handed, so the identical code round-trips
    perfectly in dev and every test passes.

    Nobody identifies a backup run more precisely than the second, so the second
    is the key. `upsert_event` writes truncated values and matches on the whole
    second, which also picks up rows written before this rule existed — whatever
    the database happened to round them to.
    """
    return value.replace(microsecond=0) if value is not None else None


class ServerCache:
    """Per-run cache so a 5,000-row import doesn't re-query for every event."""

    def __init__(self, session: Session, source: str):
        self.session = session
        self.source = source
        self._servers: dict[str, Server] = {}
        self._config: dict[str, SourceConfig] = {}
        self._settings: Settings = get_settings()

    def source_config(self) -> dict[str, SourceConfig]:
        if not self._config:
            self._config = {c.source: c for c in self.session.query(SourceConfig).all()}
        return self._config

    def get(self, raw_name: str | None) -> Server | None:
        name = normalize_name(raw_name)
        if not name:
            return None
        if name in self._servers:
            return self._servers[name]

        server = self.session.query(Server).filter(Server.name == name).one_or_none()
        if server is None:
            server = Server(
                name=name,
                display_name=str(raw_name).strip()[:255],
                primary_source=self.source,
                expected=True,
                hidden=False,
                first_seen=utcnow(),
            )
            self.session.add(server)
            self.session.flush()  # need the id for events in this same batch
        elif server.primary_source is None:
            # Pre-existing row from before the column existed, or a legacy import.
            server.primary_source = self.source
        self._servers[name] = server
        return server


def effective_timezone(
    server: Server,
    source_config: dict[str, SourceConfig],
    settings: Settings | None = None,
) -> str:
    """Per-server override -> primary source default -> app display timezone."""
    if server.timezone:
        return server.timezone
    cfg = source_config.get(server.primary_source or "")
    if cfg and cfg.default_timezone:
        return cfg.default_timezone
    return (settings or get_settings()).display_timezone


def effective_cutoff(server: Server, settings: Settings | None = None) -> int:
    if server.night_cutoff_hour is not None:
        return server.night_cutoff_hour
    value = (settings or get_settings()).night_cutoff_hour
    return value if 0 <= value <= 23 else DEFAULT_CUTOFF_HOUR


def timezone_origin(server: Server) -> str:
    """Where the server's timezone came from — surfaced in the UI so an odd
    report-day assignment can be traced without reading the code."""
    if server.timezone:
        return "override"
    return "source" if server.primary_source else "default"


def upsert_event(
    session: Session,
    cache: ServerCache,
    *,
    server: Server,
    source: str,
    start_utc: datetime,
    end_utc: datetime,
    outcome: str,
    result_raw: str | None = None,
    duration_sec: int | None = None,
    job_name: str | None = None,
    native_id: str | None = None,
    bytes_transferred: int | None = None,
    details: dict | None = None,
) -> bool:
    """Insert or update one run. Returns True when a new row was created.

    Matches on (source, server, start_utc) — the same natural key the PowerShell
    MERGE used, so a collector run and a legacy import of the same job converge
    on one row instead of duplicating it.

    The match is on the whole second (see `to_second`): the stored value has
    been through the database's own datetime precision and may not be the value
    that was handed to it.
    """
    from .models import BackupEvent

    # Before truncating — the real duration, not the truncated one.
    if duration_sec is None and start_utc and end_utc:
        duration_sec = max(0, int((end_utc - start_utc).total_seconds()))

    start_utc = to_second(start_utc)
    end_utc = to_second(end_utc)

    tz = effective_timezone(server, cache.source_config(), cache._settings)
    stamp = report_date_str(end_utc, tz, effective_cutoff(server, cache._settings))

    existing = (
        session.query(BackupEvent)
        .filter(
            BackupEvent.source == source,
            BackupEvent.server_id == server.id,
            BackupEvent.start_utc >= start_utc,
            BackupEvent.start_utc < start_utc + timedelta(seconds=1),
        )
        # Deterministic when an older row carries a rounded sub-second value.
        .order_by(BackupEvent.start_utc, BackupEvent.id)
        .first()
    )
    if existing is not None:
        # Converge the row onto the truncated key, so an estate that predates
        # this rule heals itself as its servers are re-collected.
        existing.start_utc = start_utc
        existing.end_utc = end_utc
        existing.duration_sec = duration_sec
        existing.outcome = outcome
        existing.result_raw = result_raw
        existing.report_date = stamp
        if job_name:
            existing.job_name = job_name
        if native_id:
            existing.native_id = native_id
        if bytes_transferred is not None:
            existing.bytes_transferred = bytes_transferred
        if details:
            existing.details = details
        return False

    session.add(
        BackupEvent(
            server_id=server.id,
            source=source,
            job_name=job_name,
            start_utc=start_utc,
            end_utc=end_utc,
            duration_sec=duration_sec,
            outcome=outcome,
            result_raw=result_raw,
            report_date=stamp,
            native_id=native_id,
            bytes_transferred=bytes_transferred,
            details=details,
        )
    )
    return True


def restamp_server(session: Session, server: Server) -> int:
    """Recompute report_date for every event of one server.

    Called after a timezone or cutoff change: the stamps are denormalized, so
    moving a server to London has to rewrite its history or the heatmap and the
    server page disagree about which night a job ran on.
    """
    from .models import BackupEvent

    config = {c.source: c for c in session.query(SourceConfig).all()}
    settings = get_settings()
    tz = effective_timezone(server, config, settings)
    cutoff = effective_cutoff(server, settings)

    changed = 0
    events = session.query(BackupEvent).filter(BackupEvent.server_id == server.id).all()
    for event in events:
        stamp = report_date_str(event.end_utc, tz, cutoff)
        if event.report_date != stamp:
            event.report_date = stamp
            changed += 1
    return changed
