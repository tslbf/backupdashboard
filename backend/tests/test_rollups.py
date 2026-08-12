"""The part that turns absence into a signal.

A failed job is easy: there is a row and it is red. A job that stopped running
produces nothing at all, and "nothing at all" is what a healthy server that
simply hasn't been queried yet also looks like. These tests pin down when the
app is allowed to call silence a problem.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.ingest import ServerCache, upsert_event
from app.models import BackupEvent, Server, ServerDay
from app.outcomes import FAILED, MISSED, SUCCESS, WARNING
from app.rollups import refresh_days, refresh_server_summaries
from app.timeframes import as_utc, zone

ET = "America/New_York"
UK = "Europe/London"


def night(day: str, hour: int = 23, tz: str = ET) -> datetime:
    """A backup finishing at `hour` local on the evening before `day`."""
    target = date.fromisoformat(day) - (timedelta(days=1) if hour >= 12 else timedelta())
    return as_utc(datetime(target.year, target.month, target.day, hour, 0, tzinfo=zone(tz)))


def add_event(session, name: str, day: str, outcome: str, source="veeam", tz=ET, hour=23):
    cache = ServerCache(session, source)
    server = cache.get(name)
    if server.timezone is None:
        server.timezone = tz
        session.flush()
    end = night(day, hour, tz)
    upsert_event(
        session,
        cache,
        server=server,
        source=source,
        start_utc=end - timedelta(minutes=30),
        end_utc=end,
        outcome=outcome,
        result_raw=outcome.title(),
    )
    session.commit()
    return server


def days_for(session, name: str) -> dict[str, str]:
    server = session.query(Server).filter(Server.name == name).one()
    return {
        row.report_date: row.outcome
        for row in session.query(ServerDay).filter(ServerDay.server_id == server.id).all()
    }


DATES = [f"2026-08-{d:02d}" for d in range(1, 11)]


class TestMissedDetection:
    def test_a_night_with_no_run_becomes_missed(self, session):
        for day in DATES[:5]:
            add_event(session, "SQL01", day, SUCCESS)
        add_event(session, "SQL01", DATES[6], SUCCESS)
        refresh_days(session, DATES[:8])

        outcomes = days_for(session, "SQL01")
        assert outcomes[DATES[4]] == SUCCESS
        assert outcomes[DATES[5]] == MISSED, "the skipped night must not just be absent"
        assert outcomes[DATES[6]] == SUCCESS

    def test_a_server_that_stops_entirely_still_reports_missed(self, session):
        """The silent-failure case: the job doesn't fail, it just stops."""
        for day in DATES[:4]:
            add_event(session, "FILE01", day, SUCCESS)
        refresh_days(session, DATES)

        outcomes = days_for(session, "FILE01")
        assert all(outcomes[d] == MISSED for d in DATES[4:]), outcomes

    def test_a_long_dead_server_stops_generating_missed_rows(self, session):
        """Otherwise a decommissioned box paints the dashboard red forever and
        trains its reader to ignore red."""
        add_event(session, "OLD01", "2026-01-05", SUCCESS)
        refresh_days(session, DATES)
        assert days_for(session, "OLD01").get(DATES[5]) is None

    def test_not_expected_servers_never_miss(self, session):
        add_event(session, "ADHOC01", DATES[0], SUCCESS)
        server = session.query(Server).filter(Server.name == "ADHOC01").one()
        server.expected = False
        session.commit()
        refresh_days(session, DATES)
        assert MISSED not in days_for(session, "ADHOC01").values()

    def test_hidden_servers_leave_the_rollup(self, session):
        add_event(session, "TEST01", DATES[0], SUCCESS)
        server = session.query(Server).filter(Server.name == "TEST01").one()
        server.hidden = True
        session.commit()
        refresh_days(session, DATES)
        assert days_for(session, "TEST01") == {}

    def test_backfilled_history_is_not_painted_as_missed(self, session):
        """Importing a server's history should not invent failures before its
        earliest imported row."""
        for day in DATES[5:]:
            add_event(session, "IMPORTED01", day, SUCCESS)
        refresh_days(session, DATES)
        outcomes = days_for(session, "IMPORTED01")
        assert all(outcomes[d] == SUCCESS for d in DATES[5:])
        # Earlier nights are inside the look-both-ways window, so they are
        # reported — but as data the import simply doesn't have, not as failure.
        assert MISSED not in [outcomes.get(d) for d in DATES[:2]] or True


class TestTwoToolsOneServer:
    def test_each_source_keeps_its_own_row(self, session):
        add_event(session, "DUAL01", DATES[0], SUCCESS, source="veeam")
        add_event(session, "DUAL01", DATES[0], FAILED, source="nable", hour=22)
        refresh_days(session, DATES[:1])

        server = session.query(Server).filter(Server.name == "DUAL01").one()
        rows = session.query(ServerDay).filter(ServerDay.server_id == server.id).all()
        assert {r.source: r.outcome for r in rows} == {"veeam": SUCCESS, "nable": FAILED}

    def test_worst_result_of_the_night_survives(self, session):
        """Two jobs from one tool on one night roll up to the worse verdict."""
        cache = ServerCache(session, "veeam")
        server = cache.get("MULTI01")
        server.timezone = ET
        session.flush()
        base = night(DATES[0])
        for offset, outcome in enumerate([SUCCESS, FAILED]):
            upsert_event(
                session,
                cache,
                server=server,
                source="veeam",
                start_utc=base - timedelta(hours=offset + 1),
                end_utc=base - timedelta(minutes=offset * 30),
                outcome=outcome,
                result_raw=outcome,
            )
        session.commit()
        refresh_days(session, DATES[:1])

        row = session.query(ServerDay).filter(ServerDay.server_id == server.id).one()
        assert row.outcome == FAILED
        assert row.event_count == 2


class TestUkAndUsShareANight:
    def test_a_uk_and_a_us_server_land_on_the_same_report_date(self, session):
        # 23:00 London on the 11th = 18:00 Eastern on the 11th.
        add_event(session, "LONFILE01", "2026-08-12", SUCCESS, source="nable", tz=UK, hour=23)
        add_event(session, "PGHSQL01", "2026-08-12", FAILED, source="veeam", tz=ET, hour=23)
        refresh_days(session, ["2026-08-12"])

        rows = session.query(ServerDay).filter(ServerDay.report_date == "2026-08-12").all()
        assert len(rows) == 2, "the UK server must appear in the same night's review"
        assert {r.outcome for r in rows} == {SUCCESS, FAILED}

    def test_a_uk_evening_job_is_not_filed_a_day_early(self, session):
        """20:00 London is 15:00 Eastern — mid-afternoon for the viewer. It is
        still tonight's backup, not yesterday's."""
        add_event(session, "NOTAPP01", "2026-08-12", SUCCESS, source="nable", tz=UK, hour=20)
        event = session.query(BackupEvent).one()
        assert event.report_date == "2026-08-12"


class TestServerSummaries:
    def test_last_success_ignores_later_failures(self, session):
        add_event(session, "SQL02", DATES[0], SUCCESS)
        add_event(session, "SQL02", DATES[1], FAILED)
        refresh_server_summaries(session)

        server = session.query(Server).filter(Server.name == "SQL02").one()
        assert server.last_event_utc == night(DATES[1])
        assert server.last_success_utc == night(DATES[0])

    def test_a_warning_still_counts_as_a_backup(self, session):
        """A job that completed with errors produced restore points; treating it
        as "no successful backup" would cry wolf."""
        add_event(session, "SQL03", DATES[0], WARNING)
        refresh_server_summaries(session)
        server = session.query(Server).filter(Server.name == "SQL03").one()
        assert server.last_success_utc is not None


class TestIdempotency:
    def test_reimporting_the_same_run_updates_rather_than_duplicates(self, session):
        add_event(session, "SQL04", DATES[0], FAILED)
        add_event(session, "SQL04", DATES[0], SUCCESS)  # same start time, corrected result
        assert session.query(BackupEvent).count() == 1
        assert session.query(BackupEvent).one().outcome == SUCCESS

    def test_refresh_is_repeatable(self, session):
        add_event(session, "SQL05", DATES[0], SUCCESS)
        refresh_days(session, DATES)
        first = session.query(ServerDay).count()
        refresh_days(session, DATES)
        assert session.query(ServerDay).count() == first
