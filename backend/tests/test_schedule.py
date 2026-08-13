"""One run a morning, not one an hour.

Backups finish overnight and the dashboard is read at the start of the day, so
the answer changes once per night. Polling hourly re-asks a settled question,
and against Azure in particular that is thousands of ARM requests for nothing.

The daily run is in the *viewer's* timezone rather than UTC: "before I get in"
is a local idea, and it has to stay 8am through both DST changes — which is also
why it cannot be expressed as an interval.
"""
from __future__ import annotations

import pytest

from app.collectors import ALL_COLLECTORS
from app.config import Settings


def settings(**overrides) -> Settings:
    base = {
        "collect_time": "08:00",
        "display_timezone": "America/New_York",
        "veeam_interval": 0,
        "nable_interval": 0,
        "azure_interval": 0,
    }
    base.update(overrides)
    return Settings(**base)


class TestCollectTime:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("08:00", (8, 0)),
            ("8:00", (8, 0)),
            ("08:30", (8, 30)),
            ("00:00", (0, 0)),
            ("23:59", (23, 59)),
            ("8", (8, 0)),
        ],
    )
    def test_it_parses(self, raw, expected):
        assert settings(collect_time=raw).collect_time_parts() == expected

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_means_no_daily_run(self, raw):
        assert settings(collect_time=raw).collect_time_parts() is None

    @pytest.mark.parametrize("raw", ["25:00", "08:70", "-1:00", "morning", "8am", "08:00:00"])
    def test_nonsense_disables_it_rather_than_crashing_at_startup(self, raw):
        """A bad value in .env must not stop the app booting. The scheduler
        logs a warning, because never collecting is exactly the silent failure
        this dashboard exists to notice."""
        assert settings(collect_time=raw).collect_time_parts() is None

    def test_the_default_is_eight_in_the_morning(self):
        assert Settings().collect_time_parts() == (8, 0)


class TestWhatGetsScheduled:
    def test_the_backfill_is_never_on_the_daily_run(self):
        """It walks the entire historical table. Running that every morning
        would spend minutes re-reading rows that stopped changing the day the
        collectors took over."""
        assert ALL_COLLECTORS["legacy"].schedulable(settings()) is False

    @pytest.mark.parametrize("source", ["veeam", "nable", "azure"])
    def test_the_live_sources_are(self, source):
        assert ALL_COLLECTORS[source].schedulable(settings()) is True

    @pytest.mark.parametrize("source", ["veeam", "nable", "azure"])
    def test_polling_is_off_by_default(self, source):
        assert ALL_COLLECTORS[source].interval_minutes(Settings()) == 0

    def test_an_interval_can_still_be_added_on_top(self):
        """Kept for anyone who wants a source polled more often — it is additive
        to the daily run, not a replacement for it."""
        assert ALL_COLLECTORS["veeam"].interval_minutes(settings(veeam_interval=15)) == 15


class TestScheduleLabel:
    """The Collectors page reads this string. It is built from the same
    settings the scheduler uses, so the page cannot claim a schedule that isn't
    happening."""

    def _label(self, source: str, **overrides) -> str:
        from app.api import _schedule_label

        return _schedule_label(ALL_COLLECTORS[source], settings(**overrides))

    def test_the_daily_run(self):
        assert self._label("veeam").startswith("daily at 08:00 ")

    def test_it_names_the_timezone_it_is_in(self):
        """08:00 where — the whole point of this app is that people forget to
        ask that."""
        label = self._label("veeam")
        assert "ET" in label or "EDT" in label or "EST" in label or "New_York" in label

    def test_an_interval_shows_alongside(self):
        assert self._label("veeam", veeam_interval=30) == self._label("veeam").split(" + ")[
            0
        ] + " + every 30 min"

    def test_the_backfill_says_manual(self):
        assert self._label("legacy") == "manual only"

    def test_no_daily_time_and_no_interval_says_manual(self):
        assert self._label("veeam", collect_time="") == "manual only"

    def test_no_daily_time_but_an_interval_says_the_interval(self):
        assert self._label("veeam", collect_time="", veeam_interval=60) == "every 60 min"
