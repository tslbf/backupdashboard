from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    or_,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .timeframes import utcnow


class Base(DeclarativeBase):
    pass


# Source identifiers used throughout the app. `legacy` is the historical
# BackupReporting.dbo.BackupEvents table the PowerShell scripts filled — it is a
# real source for backfill, but it never schedules and never counts as live.
SOURCES = ["veeam", "nable", "azure"]
LEGACY_SOURCE = "legacy"


class Server(Base):
    """One canonical row per protected machine, whichever tool backs it up.

    A server can legitimately be backed up by two tools (a VM in Veeam that also
    runs the Cove agent); the events stay separate, this row is the identity they
    share.
    """

    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Normalized: short name, uppercase. `display_name` keeps what the vendor said.
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))

    # The source that first reported this server. Fixed at creation and never
    # reassigned: it decides which SourceConfig timezone the server inherits, and
    # a machine that shows up in a second tool must not silently change zone.
    primary_source: Mapped[str | None] = mapped_column(String(16))

    # --- timezone: nullable on purpose ------------------------------------
    # NULL means "inherit from the primary source's default" (see SourceConfig).
    # A value here is an explicit per-server override set from the UI, and wins.
    timezone: Mapped[str | None] = mapped_column(String(64))
    # NULL means "use the app default" (settings.night_cutoff_hour). This is the
    # local hour that divides one backup night from the next.
    night_cutoff_hour: Mapped[int | None] = mapped_column(Integer)

    # Expected to back up every night. Clear it for on-demand/archive-only
    # machines so they stop generating "No backup" rows.
    expected: Mapped[bool] = mapped_column(Boolean, default=True)
    # Hidden servers leave every view and count (decommissioned, test boxes).
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    # Denormalized for cheap sorting/filtering; maintained by rollups.refresh.
    last_event_utc: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_success_utc: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    events: Mapped[list[BackupEvent]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )


class BackupEvent(Base):
    """One backup job run against one server.

    The natural key mirrors what the PowerShell MERGE used (server + start +
    source), so re-importing the legacy table and re-running a collector both
    converge instead of duplicating.
    """

    __tablename__ = "backup_events"
    __table_args__ = (
        UniqueConstraint("source", "server_id", "start_utc", name="uq_event_source_server_start"),
        Index("ix_events_report_date_source", "report_date", "source"),
        Index("ix_events_server_end", "server_id", "end_utc"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)

    job_name: Mapped[str | None] = mapped_column(String(512))
    start_utc: Mapped[datetime] = mapped_column(DateTime)
    end_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer)

    # Canonical (see outcomes.py) plus exactly what the vendor console said.
    outcome: Mapped[str] = mapped_column(String(16), index=True)
    result_raw: Mapped[str | None] = mapped_column(String(64))

    # The backup night this run belongs to, in the SERVER's timezone. Stamped at
    # write time so the hot queries are a plain indexed string compare; a
    # timezone change re-stamps via `cli recompute`.
    report_date: Mapped[str] = mapped_column(String(10), index=True)

    bytes_transferred: Mapped[int | None] = mapped_column(BigInteger)
    native_id: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict | None] = mapped_column(JSON)
    inserted_utc: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    server: Mapped[Server] = relationship(back_populates="events")


class ServerDay(Base):
    """One server's verdict for one backup night, per source.

    Materialized rather than computed per request for one reason: "No backup"
    cannot be derived from rows that do not exist. This table has a row for every
    (night, server, source) that *should* have run, so a silent absence becomes a
    queryable `missed` instead of nothing at all.
    """

    __tablename__ = "server_days"
    __table_args__ = (
        UniqueConstraint("report_date", "server_id", "source", name="uq_serverday"),
        Index("ix_serverday_date_outcome", "report_date", "outcome"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[str] = mapped_column(String(10), index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), index=True)
    source: Mapped[str] = mapped_column(String(16))

    outcome: Mapped[str] = mapped_column(String(16), index=True)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    # Longest run of the night — the number worth trending for "backups are
    # getting slower", where a sum across parallel jobs would be meaningless.
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    end_utc: Mapped[datetime | None] = mapped_column(DateTime)
    result_raw: Mapped[str | None] = mapped_column(String(64))
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SourceConfig(Base):
    """Per-source display settings and the timezone its servers default to."""

    __tablename__ = "source_config"

    source: Mapped[str] = mapped_column(String(16), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(64))
    # Servers from this source use this timezone unless the server overrides it.
    default_timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    # False for sources that only ever backfill history (legacy import).
    expected: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


# N-able/Cove protects the UK estate, so its servers run on London time — that is
# the whole reason report_date exists. Veeam (PGHVEEAM, and DUBVEEAM = Dublin,
# Ohio) and Azure are Eastern. Any exception is a per-server override.
DEFAULT_SOURCE_CONFIG = [
    ("veeam", "Veeam", "America/New_York", True, 1),
    ("nable", "N-able Cove", "Europe/London", True, 2),
    ("azure", "Azure Backup", "America/New_York", True, 3),
    (LEGACY_SOURCE, "Legacy import", "America/New_York", False, 9),
]


def seed_source_config(session) -> None:
    existing = {row.source for row in session.query(SourceConfig).all()}
    for source, name, tz, expected, order in DEFAULT_SOURCE_CONFIG:
        if source not in existing:
            session.add(
                SourceConfig(
                    source=source,
                    display_name=name,
                    default_timezone=tz,
                    expected=expected,
                    sort_order=order,
                )
            )


def visible_servers():
    """`servers.hidden` is false.

    Written as a comparison rather than `Server.hidden.is_(False)` on purpose.
    SQLAlchemy renders `.is_(False)` as `IS 0`, which SQLite happily accepts and
    SQL Server rejects outright — its `IS` takes only NULL. That difference is
    invisible in tests until the app meets a real SQL Server.

    The NULL branch covers rows written before the column existed, which
    `db._ensure_columns` adds as nullable.
    """
    return or_(Server.hidden.is_(None), Server.hidden == False)  # noqa: E712


def expected_servers():
    """`servers.expected` is true, same reasoning as above. NULL reads as
    expected, matching the column default."""
    return or_(Server.expected.is_(None), Server.expected == True)  # noqa: E712


class CollectorRun(Base):
    __tablename__ = "collector_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|success|error
    records: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
