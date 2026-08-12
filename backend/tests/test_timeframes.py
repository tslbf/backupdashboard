"""The report-day math, which is the one piece of this app that is easy to get
subtly wrong and impossible to notice: a UK server quietly missing from the
morning review looks exactly like a UK server that had a clean night."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.timeframes import (
    current_report_date,
    fmt_duration,
    night_window_utc,
    offset_label,
    report_date,
    to_local,
)

ET = "America/New_York"
UK = "Europe/London"


def utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute)


class TestOneNightAcrossTwoContinents:
    """August 2026: Eastern is UTC-4 (EDT), London is UTC+1 (BST) — five hours
    apart. All of these ran "last night" and must file under Wed 12 Aug."""

    def test_us_job_before_midnight(self):
        # 23:00 Tue Eastern = 03:00 Wed UTC
        assert report_date(utc(2026, 8, 12, 3), ET) == date(2026, 8, 12)

    def test_us_job_after_midnight(self):
        # 02:30 Wed Eastern = 06:30 Wed UTC
        assert report_date(utc(2026, 8, 12, 6, 30), ET) == date(2026, 8, 12)

    def test_uk_job_before_midnight(self):
        # 23:00 Tue London = 22:00 Tue UTC = 18:00 Tue Eastern.
        # A naive "Eastern evening" window would open after this and lose it.
        assert report_date(utc(2026, 8, 11, 22), UK) == date(2026, 8, 12)

    def test_uk_job_after_midnight(self):
        # 02:00 Wed London = 01:00 Wed UTC = 21:00 Tue Eastern
        assert report_date(utc(2026, 8, 12, 1), UK) == date(2026, 8, 12)

    def test_uk_early_evening_job(self):
        # 20:00 Tue London = 15:00 Tue Eastern — mid-afternoon for the viewer,
        # still that night's backup for the server.
        assert report_date(utc(2026, 8, 11, 19), UK) == date(2026, 8, 12)

    def test_azure_small_hours(self):
        # 03:00 Wed Eastern = 07:00 Wed UTC
        assert report_date(utc(2026, 8, 12, 7), ET) == date(2026, 8, 12)

    def test_all_five_land_on_the_same_night(self):
        events = [
            (utc(2026, 8, 12, 3), ET),
            (utc(2026, 8, 12, 6, 30), ET),
            (utc(2026, 8, 11, 22), UK),
            (utc(2026, 8, 12, 1), UK),
            (utc(2026, 8, 12, 7), ET),
        ]
        assert {report_date(when, tz) for when, tz in events} == {date(2026, 8, 12)}


class TestCutoff:
    def test_noon_is_the_divider(self):
        # 11:59 local belongs to the night that just ended...
        assert report_date(utc(2026, 8, 12, 15, 59), ET) == date(2026, 8, 12)
        # ...12:00 local starts the next one.
        assert report_date(utc(2026, 8, 12, 16), ET) == date(2026, 8, 13)

    def test_custom_cutoff_moves_the_divider(self):
        # With an 18:00 cutoff, a 16:00 local job still belongs to today.
        assert report_date(utc(2026, 8, 12, 20), ET, cutoff_hour=18) == date(2026, 8, 12)
        assert report_date(utc(2026, 8, 12, 23), ET, cutoff_hour=18) == date(2026, 8, 13)


class TestDst:
    """The offset is not a constant, and neither is the gap between the two
    regions — the US and UK change clocks on different dates."""

    def test_eastern_winter_offset(self):
        # January: Eastern is UTC-5. 23:00 Mon local = 04:00 Tue UTC.
        assert report_date(utc(2026, 1, 13, 4), ET) == date(2026, 1, 13)

    def test_uk_winter_is_utc(self):
        # January: London is UTC+0, so 23:00 local == 23:00 UTC.
        assert report_date(utc(2026, 1, 12, 23), UK) == date(2026, 1, 13)

    def test_the_transatlantic_gap_changes_in_late_march(self):
        """For two weeks a year the US/UK gap is 4 hours, not 5. A hardcoded
        offset would silently misfile every UK backup during that window."""
        # 2026: US springs forward 8 Mar, UK on 29 Mar. On 20 Mar the gap is 4h.
        march = to_local(utc(2026, 3, 20, 12), UK).utcoffset().total_seconds() - to_local(
            utc(2026, 3, 20, 12), ET
        ).utcoffset().total_seconds()
        assert march / 3600 == 4
        # In August, back to the usual 5.
        august = to_local(utc(2026, 8, 20, 12), UK).utcoffset().total_seconds() - to_local(
            utc(2026, 8, 20, 12), ET
        ).utcoffset().total_seconds()
        assert august / 3600 == 5

    def test_uk_backup_during_the_mismatch_still_lands_correctly(self):
        # 23:30 Thu 19 Mar London (UTC+0) = 23:30 UTC = 19:30 Thu Eastern (EDT).
        assert report_date(utc(2026, 3, 19, 23, 30), UK) == date(2026, 3, 20)
        assert report_date(utc(2026, 3, 19, 23, 30), ET) == date(2026, 3, 20)

    def test_spring_forward_night_has_23_hours(self):
        # US DST starts 02:00 on Sun 8 Mar 2026, which falls inside the night
        # reported as the 8th (Sat noon -> Sun noon).
        start, end = night_window_utc(date(2026, 3, 8), ET)
        assert (end - start).total_seconds() / 3600 == 23

    def test_fall_back_night_has_25_hours(self):
        # Clocks go back 02:00 on Sun 1 Nov 2026.
        start, end = night_window_utc(date(2026, 11, 1), ET)
        assert (end - start).total_seconds() / 3600 == 25

    def test_uk_and_us_transition_nights_are_different_dates(self):
        """The UK changes clocks on its own schedule; the 23/25-hour nights do
        not line up, so neither can be hardcoded from the other."""
        assert (night_window_utc(date(2026, 3, 8), UK)[1]
                - night_window_utc(date(2026, 3, 8), UK)[0]).total_seconds() / 3600 == 24
        # BST starts on the last Sunday of March: 29 Mar 2026.
        assert (night_window_utc(date(2026, 3, 29), UK)[1]
                - night_window_utc(date(2026, 3, 29), UK)[0]).total_seconds() / 3600 == 23


class TestNightWindow:
    def test_window_brackets_its_own_events(self):
        day = date(2026, 8, 12)
        start, end = night_window_utc(day, UK)
        for moment in (utc(2026, 8, 11, 22), utc(2026, 8, 12, 1), utc(2026, 8, 11, 19)):
            assert start <= moment < end
            assert report_date(moment, UK) == day

    def test_windows_tile_without_gap_or_overlap(self):
        _, first_end = night_window_utc(date(2026, 8, 12), ET)
        second_start, _ = night_window_utc(date(2026, 8, 13), ET)
        assert first_end == second_start


class TestCurrentReportDate:
    """"Last night" must not jump forward to an empty night at lunchtime."""

    def test_morning(self):
        # 09:00 Wed Eastern = 13:00 Wed UTC
        assert current_report_date(ET, utc(2026, 8, 12, 13)) == date(2026, 8, 12)

    def test_afternoon_still_means_last_night(self):
        # 16:00 Wed Eastern is past the noon cutoff, but Thursday's night has
        # barely begun — the reader still means Wednesday.
        assert current_report_date(ET, utc(2026, 8, 12, 20)) == date(2026, 8, 12)

    def test_rolls_over_at_local_midnight(self):
        # 23:59 Wed Eastern = 03:59 Thu UTC
        assert current_report_date(ET, utc(2026, 8, 13, 3, 59)) == date(2026, 8, 12)
        # 00:01 Thu Eastern
        assert current_report_date(ET, utc(2026, 8, 13, 4, 1)) == date(2026, 8, 13)


class TestLabels:
    def test_offset_label_tracks_dst(self):
        assert "UTC+1" in offset_label(UK, utc(2026, 8, 12, 12))
        assert "UTC+0" in offset_label(UK, utc(2026, 1, 12, 12))
        assert "UTC-4" in offset_label(ET, utc(2026, 8, 12, 12))
        assert "UTC-5" in offset_label(ET, utc(2026, 1, 12, 12))

    def test_unknown_zone_falls_back_instead_of_raising(self):
        # A bad hand-edit in the DB must not 500 the whole dashboard.
        assert report_date(utc(2026, 8, 12, 3), "Mars/Olympus_Mons") == date(2026, 8, 12)

    @pytest.mark.parametrize(
        "seconds,expected",
        [(None, "—"), (0, "0s"), (45, "45s"), (60, "1m"), (3600, "1h"), (12_000, "3h 20m")],
    )
    def test_duration_formatting(self, seconds, expected):
        assert fmt_duration(seconds) == expected
