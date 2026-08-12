"""Backfill from BackupReporting.dbo.BackupEvents — the table the three
PowerShell scripts have been filling.

Two things about that table shape the import:

1. **It stores Eastern local time, not UTC.** All three scripts converted to
   'Eastern Standard Time' before writing. Converting back is not just adding
   five hours: the offset depends on whether that particular timestamp fell in
   EDT or EST, so each row is localized individually via its own tzdata rules.

2. **Local time is lossy at the fall-back hour.** On the November DST night,
   01:00–02:00 Eastern happens twice and the stored value cannot say which. Those
   rows are resolved to the first (DST) occurrence — an hour of ambiguity, once a
   year, on rows that are already history. `fold` makes the choice explicit
   rather than accidental.

Rows land under the `legacy` source, so imported history is always distinguishable
from what this app collected itself, and it never generates "No backup" rows for
nights after the scripts stopped running.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from ..config import Settings
from ..ingest import ServerCache, upsert_event
from ..outcomes import normalize
from ..timeframes import as_utc, zone
from .base import Collector

log = logging.getLogger(__name__)

BATCH = 2000


def local_to_utc(value: datetime, tz_name: str) -> datetime:
    """Interpret a stored local timestamp in `tz_name` and return naive UTC."""
    if value.tzinfo is not None:
        return as_utc(value)
    # fold=0 -> the first (daylight) pass through an ambiguous repeated hour.
    return as_utc(value.replace(tzinfo=zone(tz_name), fold=0))


# The scripts wrote a Source column of 'N-Able', 'Azure', or the VBR host's short
# name (PGHVEEAM / DUBVEEAM). Map those onto this app's source vocabulary so an
# imported row is normalized by the same rules as a live one.
def map_source(raw: str | None) -> str:
    text_value = (raw or "").strip().lower()
    if not text_value:
        return "legacy"
    if "able" in text_value or "cove" in text_value:
        return "nable"
    if "azure" in text_value:
        return "azure"
    if "veeam" in text_value:
        return "veeam"
    return "legacy"


class LegacySqlCollector(Collector):
    """Reads the old table. Not scheduled by default — run it by hand:

        python -m app.cli collect legacy
    """

    source = "legacy"
    display_name = "Legacy import"

    def is_configured(self, settings: Settings) -> bool:
        return bool(settings.legacy_db_url)

    def collect(self, session: Session, settings: Settings) -> int:
        engine = create_engine(settings.legacy_db_url, pool_pre_ping=True)
        cache = ServerCache(session, self.source)
        tz_name = settings.legacy_stored_timezone
        table = settings.legacy_table

        count = 0
        skipped = 0
        with engine.connect() as connection:
            result = connection.execution_options(stream_results=True, yield_per=BATCH).execute(
                text(
                    f"SELECT ServerName, BackupStartDate, BackupEndDate, JobResult, "
                    f"DurationSec, Source FROM {table}"  # noqa: S608 — table name is operator config
                )
            )
            for row in result:
                mapping = row._mapping
                name = mapping.get("ServerName")
                end_local = mapping.get("BackupEndDate")
                start_local = mapping.get("BackupStartDate")
                if not name or end_local is None:
                    skipped += 1
                    continue

                server = cache.get(name)
                if server is None:
                    skipped += 1
                    continue

                end_utc = local_to_utc(end_local, tz_name)
                duration = mapping.get("DurationSec")
                if start_local is not None:
                    start_utc = local_to_utc(start_local, tz_name)
                elif duration:
                    start_utc = end_utc - timedelta(seconds=int(duration))
                else:
                    start_utc = end_utc

                raw_source = mapping.get("Source")
                mapped = map_source(raw_source)
                raw_result = mapping.get("JobResult")

                upsert_event(
                    session,
                    cache,
                    server=server,
                    source=mapped,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    outcome=normalize(mapped, raw_result),
                    result_raw=str(raw_result) if raw_result else None,
                    duration_sec=int(duration) if duration is not None else None,
                    job_name="Imported from BackupReporting",
                    details={"legacy_source": raw_source},
                )
                count += 1
                if count % BATCH == 0:
                    session.commit()

        session.commit()
        if skipped:
            log.info("legacy import skipped %s unusable rows", skipped)
        return count
