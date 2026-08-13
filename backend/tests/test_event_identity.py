"""A backup run is identified by its start time to the second.

That rule exists because the natural key (source, server, start_utc) is an
equality test on a datetime column, which quietly makes the *column's precision*
part of the key — and the two databases do not agree about it.

SQL Server's DATETIME keeps 1/300 of a second and rounds to it. Hand it
20:10:38.239838 and 20:10:38.240 is what comes back, so the value searched for
is never the value stored, no row is ever found, and every re-collection tries
to insert the same run again:

    [23000] Violation of UNIQUE KEY constraint 'uq_event_source_server_start'.
    Cannot insert duplicate key in object 'dbo.backup_events'. The duplicate key
    value is (azure, 55, 2026-08-12 20:10:38.240). (2627)

SQLite stores exactly what it is handed, so in dev the same code round-trips
perfectly and every test passes. These tests reproduce the rounding rather than
the database, so they fail on SQLite too.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.ingest import ServerCache, existing_event_keys, to_second, upsert_event
from app.models import BackupEvent


def sqlserver_datetime(value: datetime) -> datetime:
    """What DATETIME gives back after storing `value`.

    Ticks are 1/300 s, so the fractional part lands on one of .000, .003, .007
    and so on, and the value is rounded to the nearest.
    """
    ticks = round(value.microsecond / 1_000_000 * 300)
    if ticks == 300:  # rounded up into the next second
        return value.replace(microsecond=0) + timedelta(seconds=1)
    return value.replace(microsecond=int(ticks / 300 * 1_000_000))


class TestTheRoundingItself:
    def test_the_reported_value_is_what_sql_server_did(self):
        """The exact numbers out of the AZUSCCM01 traceback."""
        collected = datetime(2026, 8, 12, 20, 10, 38, 239838)
        assert sqlserver_datetime(collected) == datetime(2026, 8, 12, 20, 10, 38, 240000)
        assert collected != sqlserver_datetime(collected), (
            "if these were equal there would have been no bug to fix"
        )

    def test_a_whole_second_survives_untouched(self):
        """Which is the entire point of truncating before the write: .000 is
        exactly representable, so what goes in is what comes back."""
        value = datetime(2026, 8, 12, 20, 10, 38)
        assert sqlserver_datetime(value) == value


class TestUpsertKeepsSeconds:
    def _cache(self, session):
        return ServerCache(session, "azure")

    def _write(self, session, cache, start: datetime, **kwargs) -> bool:
        server = cache.get("AZSQL01")
        return upsert_event(
            session,
            cache,
            server=server,
            source="azure",
            start_utc=start,
            end_utc=start + timedelta(minutes=93),
            outcome=kwargs.pop("outcome", "success"),
            **kwargs,
        )

    def test_sub_second_precision_is_dropped_on_write(self, session):
        cache = self._cache(session)
        self._write(session, cache, datetime(2026, 8, 12, 20, 10, 38, 239838))
        session.commit()

        event = session.query(BackupEvent).one()
        assert event.start_utc == datetime(2026, 8, 12, 20, 10, 38)
        assert event.start_utc.microsecond == 0
        assert event.end_utc.microsecond == 0

    def test_recollecting_the_same_run_updates_instead_of_inserting(self, session):
        """The failure as it actually happened: the row is already in the
        database, rounded, and the collector runs again an hour later."""
        cache = self._cache(session)
        collected = datetime(2026, 8, 12, 20, 10, 38, 239838)

        # Seed the row the way SQL Server would have stored it before this fix.
        server = cache.get("AZSQL01")
        session.add(
            BackupEvent(
                server_id=server.id,
                source="azure",
                start_utc=sqlserver_datetime(collected),
                end_utc=sqlserver_datetime(collected) + timedelta(minutes=93),
                outcome="running",
                report_date="2026-08-13",
            )
        )
        session.flush()

        created = self._write(session, cache, collected, outcome="success")
        session.commit()

        assert created is False, "it must recognise the run it already has"
        event = session.query(BackupEvent).one()
        assert event.outcome == "success", "and take the newer verdict"
        assert event.start_utc == datetime(2026, 8, 12, 20, 10, 38), (
            "the row should converge onto the truncated key, so an estate that "
            "predates this rule heals as it is re-collected"
        )

    def test_two_runs_a_second_apart_stay_two_runs(self, session):
        """The window is one second wide and half-open — a real neighbouring
        run must not be swallowed by it."""
        cache = self._cache(session)
        first = datetime(2026, 8, 12, 20, 10, 38, 900000)
        second = datetime(2026, 8, 12, 20, 10, 39, 100000)

        assert self._write(session, cache, first) is True
        assert self._write(session, cache, second) is True
        session.commit()

        assert session.query(BackupEvent).count() == 2

    def test_repeated_collection_is_idempotent(self, session):
        """Four runs of the collector over the same job, each with whatever
        sub-second value the vendor happened to report."""
        cache = self._cache(session)
        base = datetime(2026, 8, 12, 20, 10, 38)
        for microsecond in (239838, 240000, 0, 999999):
            self._write(session, cache, base.replace(microsecond=microsecond))
        session.commit()

        assert session.query(BackupEvent).count() == 1

    def test_duration_is_measured_before_the_truncation(self, session):
        """Truncating both ends and then subtracting would lose up to a second
        of a job that is timed to the millisecond."""
        cache = self._cache(session)
        server = cache.get("AZSQL01")
        start = datetime(2026, 8, 12, 20, 10, 38, 900000)
        upsert_event(
            session,
            cache,
            server=server,
            source="azure",
            start_utc=start,
            end_utc=start + timedelta(seconds=59, microseconds=200000),
            outcome="success",
        )
        session.commit()

        assert session.query(BackupEvent).one().duration_sec == 59


class TestToSecond:
    def test_none_passes_through(self):
        assert to_second(None) is None

    def test_it_is_idempotent(self):
        once = to_second(datetime(2026, 8, 12, 20, 10, 38, 239838))
        assert to_second(once) == once


@pytest.mark.parametrize("microsecond", [0, 1, 239838, 500000, 900000, 998000])
def test_truncation_lands_inside_the_lookup_window(microsecond):
    """The invariant the upsert's range filter rests on: whatever SQL Server
    stored, it sits inside [truncated, truncated + 1s)."""
    value = datetime(2026, 8, 12, 20, 10, 38, microsecond)
    stored = sqlserver_datetime(value)
    key = to_second(value)
    assert key <= stored < key + timedelta(seconds=1)


def test_the_one_gap_in_that_invariant_is_known_and_narrow():
    """Above .998334, DATETIME rounds up into the *next* second, so a row
    written before this fix sits outside the window derived from the same
    reading and would be re-inserted rather than matched.

    Pinned rather than fixed: widening the window to catch it would let a real
    run one second later be swallowed instead, which is the worse trade. It can
    only affect rows written before the truncation rule, and only for the
    0.17% of readings that fall in that sliver.
    """
    value = datetime(2026, 8, 12, 20, 10, 38, 999900)
    stored = sqlserver_datetime(value)

    assert stored == datetime(2026, 8, 12, 20, 10, 39)
    assert stored >= to_second(value) + timedelta(seconds=1), "outside the window, as described"


class TestTheBackfillFastPath:
    """The legacy import walks the whole historical table, and `upsert_event`
    normally asks the database whether each row exists — hundreds of thousands
    of network round trips, which is what made it look hung.

    `known_keys` answers all of them from one query up front. It has to be
    exactly as correct as the lookup it replaces.
    """

    def _cache(self, session):
        return ServerCache(session, "legacy")

    def _write(self, session, cache, start, known=None, outcome="success"):
        server = cache.get("OLDBOX")
        return upsert_event(
            session,
            cache,
            server=server,
            source="veeam",
            start_utc=start,
            end_utc=start + timedelta(minutes=20),
            outcome=outcome,
            known_keys=known,
        )

    def test_it_finds_what_is_already_there(self, session):
        cache = self._cache(session)
        start = datetime(2026, 5, 1, 23, 0, 0)
        self._write(session, cache, start)
        session.commit()

        known = existing_event_keys(session)
        created = self._write(session, cache, start, known=known, outcome="failed")
        session.commit()

        assert created is False
        assert session.query(BackupEvent).count() == 1
        assert session.query(BackupEvent).one().outcome == "failed"

    def test_a_second_copy_inside_one_import_is_still_caught(self, session):
        """The set is kept up to date as rows are inserted — otherwise a table
        with the same run recorded twice would violate the unique constraint
        the moment it flushed."""
        cache = self._cache(session)
        known = existing_event_keys(session)
        start = datetime(2026, 5, 1, 23, 0, 0)

        assert self._write(session, cache, start, known=known) is True
        assert self._write(session, cache, start, known=known) is False
        session.commit()

        assert session.query(BackupEvent).count() == 1

    def test_the_keys_are_truncated_like_everything_else(self, session):
        """A row written before the second-precision rule still has to be
        recognised, or the import would duplicate it."""
        cache = self._cache(session)
        server = cache.get("OLDBOX")
        session.add(
            BackupEvent(
                server_id=server.id,
                source="veeam",
                start_utc=datetime(2026, 5, 1, 23, 0, 0, 240000),
                end_utc=datetime(2026, 5, 1, 23, 20, 0),
                outcome="success",
                report_date="2026-05-02",
            )
        )
        session.commit()

        known = existing_event_keys(session)
        assert (("veeam", server.id, datetime(2026, 5, 1, 23, 0, 0))) in known

        created = self._write(session, cache, datetime(2026, 5, 1, 23, 0, 0, 239838), known=known)
        session.commit()

        assert created is False
        assert session.query(BackupEvent).count() == 1

    def test_without_the_hint_the_behaviour_is_identical(self, session):
        """The fast path must be an optimisation and nothing more."""
        cache = self._cache(session)
        start = datetime(2026, 5, 1, 23, 0, 0)

        self._write(session, cache, start)
        session.commit()
        with_hint = self._write(session, cache, start, known=existing_event_keys(session))
        without = self._write(session, cache, start)
        session.commit()

        assert with_hint is False and without is False
        assert session.query(BackupEvent).count() == 1
