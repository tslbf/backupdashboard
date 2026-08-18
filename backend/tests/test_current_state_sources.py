"""A source that reports current state can only be judged on nights it saw.

Cove's `EnumerateAccountStatistics` returns each device's *latest* session and
nothing else — no date range, no history. So the collector can only ever learn
what the state is at the moment it asks, and a night nobody asked about is not
an empty night; it is an unknown one.

Treating the two the same produced the bug this file exists to prevent: a
weekend with the collector switched off came back as a solid band of "No backup"
across the entire UK estate, every one of which had in fact backed up fine — and
the morning digest would have emailed it.

Veeam and Azure are untouched by any of this. Both have real history endpoints,
so asking for 96 hours and finding a night absent means it genuinely was absent.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models import CollectorRun, Server, ServerDay
from app.outcomes import MISSED, SUCCESS
from app.rollups import CURRENT_STATE_SOURCES, refresh_days
from app.timeframes import as_utc, zone

from test_rollups import ET, UK, add_event

# Four nights, with the middle two the "weekend" the collector slept through.
NIGHTS = ["2026-08-13", "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17"]
FRIDAY, SATURDAY, SUNDAY, MONDAY = NIGHTS[1], NIGHTS[2], NIGHTS[3], NIGHTS[4]


def ran(session, day: str, hour: int, tz: str = ET, source: str = "nable", status="success"):
    """Record a collector run at `hour` local on `day`'s calendar date."""
    when = as_utc(
        datetime(*[int(p) for p in day.split("-")], hour, 0, tzinfo=zone(tz))
    )
    session.add(
        CollectorRun(
            source=source,
            status=status,
            started_at=when,
            finished_at=when + timedelta(seconds=20),
            records=1,
        )
    )
    session.commit()


def outcomes(session, name: str) -> dict[str, str]:
    server = session.query(Server).filter(Server.name == name).one()
    rows = (
        session.query(ServerDay)
        .filter(ServerDay.server_id == server.id, ServerDay.report_date.in_(NIGHTS))
        .all()
    )
    return {r.report_date: r.outcome for r in rows}


@pytest.fixture
def cove_gap(session):
    """A UK Cove server that backed up either side of the weekend."""
    add_event(session, "LONFILE01", NIGHTS[0], SUCCESS, source="nable", tz=UK, hour=23)
    add_event(session, "LONFILE01", FRIDAY, SUCCESS, source="nable", tz=UK, hour=23)
    add_event(session, "LONFILE01", MONDAY, SUCCESS, source="nable", tz=UK, hour=23)
    return session


class TestTheWeekendTheCollectorSlept:
    def test_unobserved_nights_are_not_called_missed(self, cove_gap):
        """No collector runs at all: nothing can be asserted about any night."""
        refresh_days(cove_gap, NIGHTS)

        found = outcomes(cove_gap, "LONFILE01")
        assert SATURDAY not in found
        assert SUNDAY not in found
        assert found[FRIDAY] == SUCCESS and found[MONDAY] == SUCCESS

    def test_a_night_that_was_polled_is_still_called_missed(self, cove_gap):
        """The guard suppresses unknowns, not real absences — a night the
        collector looked at and found nothing is a genuine miss."""
        ran(cove_gap, SATURDAY, 8, tz=UK)
        refresh_days(cove_gap, NIGHTS)

        found = outcomes(cove_gap, "LONFILE01")
        assert found[SATURDAY] == MISSED
        assert SUNDAY not in found, "still nobody looked on the Sunday"

    def test_a_failed_run_observed_nothing(self, cove_gap):
        """A run that errored may never have reached the API at all."""
        ran(cove_gap, SATURDAY, 8, tz=UK, status="error")
        refresh_days(cove_gap, NIGHTS)

        assert SATURDAY not in outcomes(cove_gap, "LONFILE01")

    def test_the_real_problem_still_surfaces(self, session):
        """The case that must not be broken by any of this: a device that has
        genuinely stopped, on nights the collector was running throughout."""
        for day in NIGHTS[:2]:
            add_event(session, "NOTCODEBEAMER", day, SUCCESS, source="nable", tz=UK, hour=23)
        for day in NIGHTS:
            ran(session, day, 8, tz=UK)
        refresh_days(session, NIGHTS)

        found = outcomes(session, "NOTCODEBEAMER")
        assert found[SATURDAY] == MISSED
        assert found[SUNDAY] == MISSED
        assert found[MONDAY] == MISSED


class TestWhatARunCanSee:
    def test_a_morning_run_covers_the_night_it_is_inside(self, cove_gap):
        """Nights run noon to noon, so an 08:00 poll sits inside the night whose
        backups have just finished."""
        ran(cove_gap, SATURDAY, 8, tz=UK)
        refresh_days(cove_gap, NIGHTS)

        assert outcomes(cove_gap, "LONFILE01")[SATURDAY] == MISSED

    def test_an_afternoon_run_covers_the_night_that_just_closed(self, cove_gap):
        """15:00 has rolled into the next night, but the poll still saw the one
        that ended at noon — so it can speak for it."""
        ran(cove_gap, SATURDAY, 15, tz=UK)
        refresh_days(cove_gap, NIGHTS)

        found = outcomes(cove_gap, "LONFILE01")
        assert found[SATURDAY] == MISSED, "the night that closed three hours earlier"

    def test_a_run_says_nothing_about_the_week_before(self, session):
        """The whole point: Cove has long since overwritten those sessions."""
        add_event(session, "LONFILE01", NIGHTS[0], SUCCESS, source="nable", tz=UK, hour=23)
        add_event(session, "LONFILE01", MONDAY, SUCCESS, source="nable", tz=UK, hour=23)
        ran(session, MONDAY, 8, tz=UK)
        refresh_days(session, NIGHTS)

        found = outcomes(session, "LONFILE01")
        assert FRIDAY not in found and SATURDAY not in found
        assert SUNDAY in found, "only the night immediately before the poll"


class TestTimezonesStillDecideTheNight:
    def test_one_run_lands_on_different_nights_for_different_estates(self, session):
        """A single 08:00 Eastern poll is 13:00 in London — the same instant,
        different backup nights. This is the app's premise, and it survives into
        which nights a poll is allowed to speak for."""
        add_event(session, "LONFILE01", NIGHTS[0], SUCCESS, source="nable", tz=UK, hour=23)
        add_event(session, "LONFILE01", MONDAY, SUCCESS, source="nable", tz=UK, hour=23)
        add_event(session, "PGHFILE01", NIGHTS[0], SUCCESS, source="nable", tz=ET, hour=23)
        add_event(session, "PGHFILE01", MONDAY, SUCCESS, source="nable", tz=ET, hour=23)

        # 08:00 Eastern on the Sunday = 13:00 London, already into Monday there.
        ran(session, SUNDAY, 8, tz=ET)
        refresh_days(session, NIGHTS)

        uk = outcomes(session, "LONFILE01")
        us = outcomes(session, "PGHFILE01")
        # London: 13:00 is past noon, so the poll sits in the MONDAY night and
        # speaks for Monday and the Sunday that closed an hour earlier.
        assert SUNDAY in uk and SATURDAY not in uk
        # Eastern: 08:00 is before noon, so the poll sits in the SUNDAY night
        # and speaks for Sunday and Saturday. Same instant, different nights.
        assert SUNDAY in us and SATURDAY in us


class TestEverySourceElse:
    def test_veeam_needs_no_observation_to_report_a_miss(self, session):
        """Veeam has a real history endpoint. A 96-hour query that comes back
        without a night is evidence, not silence — so nothing here applies."""
        assert "veeam" not in CURRENT_STATE_SOURCES

        add_event(session, "PGHSQL01", NIGHTS[0], SUCCESS)
        add_event(session, "PGHSQL01", MONDAY, SUCCESS)
        refresh_days(session, NIGHTS)

        found = outcomes(session, "PGHSQL01")
        assert found[SATURDAY] == MISSED
        assert found[SUNDAY] == MISSED

    def test_azure_likewise(self, session):
        assert "azure" not in CURRENT_STATE_SOURCES

        add_event(session, "AZUSQL01", NIGHTS[0], SUCCESS, source="azure")
        add_event(session, "AZUSQL01", MONDAY, SUCCESS, source="azure")
        refresh_days(session, NIGHTS)

        assert outcomes(session, "AZUSQL01")[SATURDAY] == MISSED
