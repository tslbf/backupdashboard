from __future__ import annotations

from datetime import timedelta

import pytest

from fastapi import HTTPException

from app.api import (
    BulkServerUpdate,
    ServerUpdate,
    bulk_update_servers,
    day_detail,
    overview,
    server_detail,
    trends,
    update_server,
)
from app.models import BackupEvent, Server
from app.outcomes import FAILED, MISSED, SUCCESS
from app.rollups import refresh_days, refresh_server_summaries
from app.timeframes import current_report_date

from test_rollups import ET, UK, add_event

TODAY = current_report_date("America/New_York")
NIGHTS = [(TODAY - timedelta(days=n)).isoformat() for n in range(6, -1, -1)]


@pytest.fixture
def estate(session):
    """Two clean servers, one broken, one that went dark, one UK."""
    for day in NIGHTS:
        add_event(session, "PGHSQL01", day, SUCCESS)
        add_event(session, "PGHAPP01", day, SUCCESS)
        add_event(session, "PGHERP02", day, FAILED)
        add_event(session, "LONFILE01", day, SUCCESS, source="nable", tz=UK, hour=23)
    for day in NIGHTS[:4]:
        add_event(session, "BEDAPP01", day, SUCCESS)
    refresh_server_summaries(session)
    refresh_days(session, NIGHTS)
    return session


class TestOverview:
    def test_counts_last_night(self, estate):
        data = overview(date_param=None, session=estate)
        assert data["report_date"] == TODAY.isoformat()
        assert data["counts"][SUCCESS] == 3
        assert data["counts"][FAILED] == 1
        assert data["counts"][MISSED] == 1  # BEDAPP01 stopped running

    def test_problems_are_ordered_worst_first(self, estate):
        problems = overview(date_param=None, session=estate)["problems"]
        assert [p["outcome"] for p in problems] == [FAILED, MISSED]
        assert problems[0]["server"] == "PGHERP02"

    def test_chronic_failure_carries_its_streak(self, estate):
        problems = overview(date_param=None, session=estate)["problems"]
        broken = next(p for p in problems if p["server"] == "PGHERP02")
        assert broken["streak"] == len(NIGHTS)

    def test_uk_server_is_in_tonights_numbers(self, estate):
        """The whole point: a 23:00 London backup is 18:00 Eastern, and it still
        belongs to the night the viewer is looking at."""
        rows = day_detail(TODAY.isoformat(), session=estate)["rows"]
        uk = next(r for r in rows if r["server"] == "LONFILE01")
        assert uk["timezone"] == UK
        assert uk["end_local"] == "23:00"

    def test_protected_percentage_counts_servers_not_jobs(self, estate):
        data = overview(date_param=None, session=estate)
        assert data["servers_total"] == 5
        assert data["servers_protected"] == 3
        assert data["protected_pct"] == 60.0

    def test_an_explicit_date_reads_history(self, estate):
        data = overview(date_param=NIGHTS[0], session=estate)
        assert data["report_date"] == NIGHTS[0]
        assert data["is_current"] is False
        assert data["counts"][MISSED] == 0  # BEDAPP01 was still running then

    def test_bad_date_is_rejected(self, estate):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            overview(date_param="last-tuesday", session=estate)
        assert exc.value.status_code == 400


class TestTrends:
    def test_one_point_per_night_even_when_empty(self, estate):
        series = trends(days=30, source=None, session=estate)
        assert len(series) == 30
        assert series[-1]["date"] == TODAY.isoformat()

    def test_success_rate_is_present_where_there_is_data(self, estate):
        series = trends(days=7, source=None, session=estate)
        assert series[-1]["success_rate"] is not None

    def test_filtering_by_source_isolates_it(self, estate):
        series = trends(days=7, source="nable", session=estate)
        assert series[-1][SUCCESS] == 1


class TestServerEditing:
    def test_an_overnight_job_keeps_its_night_when_reassigned_to_london(self, estate):
        """Worth pinning: 23:00 Eastern is 04:00 London, and both readings put
        the run in the same backup night. Getting a server's timezone wrong
        therefore does *not* silently shuffle ordinary overnight backups between
        nights — which is exactly why the misfiling this app guards against is
        invisible without the report-day model."""
        server = estate.query(Server).filter(Server.name == "PGHSQL01").one()
        before = {
            e.id: e.report_date
            for e in estate.query(BackupEvent).filter(BackupEvent.server_id == server.id)
        }
        result = update_server(server.id, ServerUpdate(timezone=UK), session=estate)

        assert result["timezone"] == UK
        assert result["timezone_origin"] == "override"
        assert result["events_restamped"] == 0
        after = {
            e.id: e.report_date
            for e in estate.query(BackupEvent).filter(BackupEvent.server_id == server.id)
        }
        assert after == before

    def test_a_daytime_job_does_move_night_when_reassigned(self, estate):
        """An 08:00 Eastern job is 13:00 London — past the noon cutoff, so it
        belongs to the *next* night once the server is read as UK. The stored
        stamps have to follow, or the heatmap and the event list disagree."""
        add_event(session=estate, name="ODD01", day=NIGHTS[-1], outcome=SUCCESS, hour=8)
        refresh_days(estate, NIGHTS)
        server = estate.query(Server).filter(Server.name == "ODD01").one()
        before = estate.query(BackupEvent).filter(BackupEvent.server_id == server.id).one()
        original = before.report_date

        result = update_server(server.id, ServerUpdate(timezone=UK), session=estate)
        assert result["events_restamped"] == 1

        after = estate.query(BackupEvent).filter(BackupEvent.server_id == server.id).one()
        assert after.report_date != original

    def test_clearing_the_override_returns_to_the_source_default(self, estate):
        server = estate.query(Server).filter(Server.name == "LONFILE01").one()
        update_server(server.id, ServerUpdate(timezone=ET), session=estate)
        result = update_server(server.id, ServerUpdate(clear_timezone=True), session=estate)
        assert result["timezone"] == UK  # nable's seeded default
        assert result["timezone_origin"] == "source"

    def test_an_unknown_timezone_is_rejected(self, estate):
        from fastapi import HTTPException

        server = estate.query(Server).filter(Server.name == "PGHSQL01").one()
        with pytest.raises(HTTPException) as exc:
            update_server(server.id, ServerUpdate(timezone="Europe/Nottingham"), session=estate)
        assert exc.value.status_code == 400

    def test_marking_not_expected_clears_its_missed_nights(self, estate):
        server = estate.query(Server).filter(Server.name == "BEDAPP01").one()
        assert overview(date_param=None, session=estate)["counts"][MISSED] == 1
        update_server(server.id, ServerUpdate(expected=False), session=estate)
        assert overview(date_param=None, session=estate)["counts"][MISSED] == 0


class TestServerDetail:
    def test_timeline_covers_every_night_in_range(self, estate):
        server = estate.query(Server).filter(Server.name == "PGHERP02").one()
        detail = server_detail(server.id, days=30, session=estate)
        assert len(detail["timeline"]) == 30
        assert detail["streak"] == len(NIGHTS)

    def test_events_carry_both_clocks(self, estate):
        server = estate.query(Server).filter(Server.name == "LONFILE01").one()
        detail = server_detail(server.id, days=30, session=estate)
        event = detail["events"][0]
        assert event["end_local"] == "23:00"
        assert event["end_utc"] is not None

    def test_missing_server_404s(self, estate):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            server_detail(999_999, days=30, session=estate)
        assert exc.value.status_code == 404


class TestBulkHide:
    """Hiding several servers at once.

    Its own endpoint rather than a loop over PATCH for one reason: `hidden` and
    `expected` both feed missed-detection, so each change has to be followed by
    a rollup rebuild. Per server, a forty-server selection would rebuild the
    whole estate's nights forty times.
    """

    def _ids(self, session, *names) -> list[int]:
        return [session.query(Server).filter(Server.name == n).one().id for n in names]

    def test_it_hides_every_server_named(self, estate):
        ids = self._ids(estate, "PGHSQL01", "PGHAPP01")

        result = bulk_update_servers(
            BulkServerUpdate(server_ids=ids, hidden=True), session=estate
        )

        assert result["updated"] == 2
        assert all(estate.get(Server, i).hidden for i in ids)

    def test_hidden_servers_leave_the_night(self, estate):
        """The point of hiding: the counts stop including them."""
        before = overview(date_param=None, session=estate)["servers_total"]
        bulk_update_servers(
            BulkServerUpdate(server_ids=self._ids(estate, "PGHERP02"), hidden=True),
            session=estate,
        )
        after = overview(date_param=None, session=estate)

        assert after["servers_total"] == before - 1
        assert after["counts"][FAILED] == 0, "the failing server was the one hidden"

    def test_unhiding_brings_them_back(self, estate):
        ids = self._ids(estate, "PGHERP02")
        bulk_update_servers(BulkServerUpdate(server_ids=ids, hidden=True), session=estate)
        bulk_update_servers(BulkServerUpdate(server_ids=ids, hidden=False), session=estate)

        assert overview(date_param=None, session=estate)["counts"][FAILED] == 1

    def test_a_repeated_id_is_counted_once(self, estate):
        ids = self._ids(estate, "PGHSQL01")
        result = bulk_update_servers(
            BulkServerUpdate(server_ids=ids * 3, hidden=True), session=estate
        )
        assert result["updated"] == 1

    def test_unknown_ids_are_ignored_rather_than_fatal(self, estate):
        """A stale selection — the page was open while a purge ran — should
        still apply to the servers that do exist."""
        ids = self._ids(estate, "PGHSQL01") + [999_999]

        result = bulk_update_servers(
            BulkServerUpdate(server_ids=ids, hidden=True), session=estate
        )

        assert result["updated"] == 1

    def test_an_empty_selection_is_rejected(self, estate):
        with pytest.raises(HTTPException) as exc:
            bulk_update_servers(BulkServerUpdate(server_ids=[], hidden=True), session=estate)
        assert exc.value.status_code == 400

    def test_a_change_with_no_fields_is_rejected(self, estate):
        """Otherwise it would rebuild every night in the estate for nothing."""
        with pytest.raises(HTTPException) as exc:
            bulk_update_servers(
                BulkServerUpdate(server_ids=self._ids(estate, "PGHSQL01")), session=estate
            )
        assert exc.value.status_code == 400

    def test_expected_can_be_cleared_in_bulk_too(self, estate):
        """A server that is not expected stops generating "No backup" rows —
        the softer alternative to hiding it outright."""
        ids = self._ids(estate, "BEDAPP01")
        assert overview(date_param=None, session=estate)["counts"][MISSED] == 1

        bulk_update_servers(BulkServerUpdate(server_ids=ids, expected=False), session=estate)

        assert overview(date_param=None, session=estate)["counts"][MISSED] == 0
