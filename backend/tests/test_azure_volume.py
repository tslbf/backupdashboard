"""Sixty servers should not produce thirty thousand backup events.

Two things make an Azure collection run away, and from the outside they look
identical — a counter going up for several minutes:

  * **Transaction-log backups.** SQL Server and SAP HANA in a VM back their logs
    up every 15 minutes, per database, and ARM reports each one as
    `operation: Backup`. No server-side filter separates them from the nightly
    full, and one database alone contributes ~384 of them to a 96-hour window.
  * **An ignored `$filter`.** ARM is not obliged to honour it. When it doesn't,
    the collector pages the vault's entire retained history rather than the
    window it asked for.

The first is filtered out by default; the second is enforced client-side and
counted, so it shows up as a number in the log instead of as a collection that
never finishes.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import unquote

import httpx
import pytest

from app.collectors.azure import AzureCollector, backup_type, is_log_backup
from app.config import get_settings
from app.ingest import ServerCache
from app.models import BackupEvent, Server
from app.timeframes import utcnow


def job(
    entity: str,
    start: datetime,
    *,
    kind: str = "AzureWorkload",
    backup: str | None = None,
    minutes: int = 30,
    status: str = "Completed",
) -> dict:
    properties = {
        "entityFriendlyName": entity,
        "backupManagementType": kind,
        "operation": "Backup",
        "status": status,
        "startTime": start.isoformat() + "Z",
        "endTime": (start + timedelta(minutes=minutes)).isoformat() + "Z",
    }
    if backup is not None:
        properties["extendedInfo"] = {"propertyBag": {"Backup Type": backup}}
    return {"name": f"{entity}-{start:%Y%m%d%H%M%S}", "properties": properties}


class TestBackupType:
    def test_it_reads_the_property_bag(self):
        assert backup_type(job("DB1", utcnow(), backup="Log")["properties"]) == "Log"

    def test_a_job_without_one_is_not_a_log_backup(self):
        """An IaaS VM or a file share has only one kind of backup and says
        nothing about it — those must not be swept up by the filter."""
        properties = job("VM1", utcnow(), kind="AzureIaasVM")["properties"]
        assert backup_type(properties) is None
        assert is_log_backup(properties) is False

    @pytest.mark.parametrize("value", ["Log", "log", "LOG"])
    def test_case_does_not_matter(self, value):
        assert is_log_backup(job("DB1", utcnow(), backup=value)["properties"]) is True

    @pytest.mark.parametrize("value", ["Full", "Differential", "Incremental"])
    def test_the_nightly_kinds_are_kept(self, value):
        assert is_log_backup(job("DB1", utcnow(), backup=value)["properties"]) is False


class TestTheFilterSentToArm:
    """backupJobs' `$filter` is not ordinary OData, and gives no sign when it
    is wrong: a filter it cannot parse returns HTTP 200 and the vault's entire
    retained job history. That is how a 58-server estate answered a 24-hour
    question with 60,252 records."""

    def _filter(self) -> str:
        url = AzureCollector()._jobs_url(
            "sub",
            "rg",
            "vault",
            datetime(2026, 8, 9, 13, 30, 0),
            datetime(2026, 8, 13, 9, 5, 0),
        )
        return unquote(url).partition("$filter=")[2]

    def test_the_range_is_expressed_with_eq(self):
        """`startTime eq X and endTime eq Y` means "between X and Y" here.
        `ge`/`le` read like the correct thing to write and are ignored."""
        decoded = self._filter()

        assert "startTime eq '" in decoded
        assert "endTime eq '" in decoded
        assert " ge " not in decoded and " le " not in decoded

    def test_the_timestamps_are_twelve_hour_with_am_pm(self):
        """Not ISO 8601. `2026-08-09 01:30:00 PM`, space-separated."""
        decoded = self._filter()

        assert "startTime eq '2026-08-09 01:30:00 PM'" in decoded
        assert "endTime eq '2026-08-13 09:05:00 AM'" in decoded
        assert "T" not in decoded.replace("startTime", "").replace("endTime", "")

    def test_the_operation_clause_is_still_there(self):
        assert "operation eq 'Backup'" in self._filter()

    @pytest.mark.parametrize(
        "hour,expected",
        [(0, "12:00:00 AM"), (1, "01:00:00 AM"), (11, "11:00:00 AM"),
         (12, "12:00:00 PM"), (13, "01:00:00 PM"), (23, "11:00:00 PM")],
    )
    def test_midnight_and_noon_are_the_ones_that_go_wrong(self, hour, expected):
        from app.collectors.azure import _arm_time

        assert _arm_time(datetime(2026, 8, 9, hour, 0, 0)).endswith(expected)

    def test_am_pm_is_not_left_to_strftime(self):
        """`%p` is locale-dependent — on a Windows box with a non-English
        locale it comes back translated or empty, and the filter would be
        quietly ignored all over again.

        Checked against the parsed source with its docstring removed, because
        the docstring explains the trap and would otherwise match itself.
        """
        import ast
        import inspect
        import textwrap

        from app.collectors.azure import _arm_time

        tree = ast.parse(textwrap.dedent(inspect.getsource(_arm_time)))
        function = tree.body[0]
        if ast.get_docstring(function):
            function.body = function.body[1:]

        literals = [
            node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        assert not any("%p" in text for text in literals), literals


class TestCollectionVolume:
    """The shape of a real estate: a handful of VMs backed up nightly, and SQL
    databases whose logs go every 15 minutes."""

    def _vault(self, session, jobs: list[dict], monkeypatch, page_size=1000, **settings_overrides):
        collector = AzureCollector()
        now = utcnow()

        def fake_pages(self, client, url, label=""):
            for start in range(0, max(len(jobs), 1), page_size):
                yield jobs[start : start + page_size]

        monkeypatch.setattr(AzureCollector, "_iter_pages", fake_pages)

        cache = ServerCache(session, "azure")
        for key, value in settings_overrides.items():
            monkeypatch.setattr(cache._settings, key, value)

        return collector._collect_vault(
            session,
            httpx.Client(),
            cache,
            "sub",
            "rg",
            "vault",
            now - timedelta(hours=96),
            now,
        )

    def test_log_backups_are_not_stored(self, session, monkeypatch):
        now = utcnow()
        jobs = [job("SQLVM01", now - timedelta(hours=8), backup="Full")]
        # Four days of a single database's transaction logs.
        jobs += [
            job("PAYROLL", now - timedelta(minutes=15 * n), backup="Log", minutes=1)
            for n in range(1, 385)
        ]

        stored = self._vault(session, jobs, monkeypatch)
        session.commit()

        assert stored == 1, "only the nightly full belongs on a nightly dashboard"
        assert session.query(BackupEvent).count() == 1

    def test_turning_them_back_on_stores_them(self, session, monkeypatch):
        now = utcnow()
        jobs = [
            job("PAYROLL", now - timedelta(minutes=15 * n), backup="Log", minutes=1)
            for n in range(1, 5)
        ]

        stored = self._vault(session, jobs, monkeypatch, azure_include_log_backups=True)
        session.commit()

        assert stored == 4

    def test_history_outside_the_window_is_dropped(self, session, monkeypatch):
        """What an ignored $filter looks like: ARM hands back a year of jobs for
        a 96-hour question."""
        now = utcnow()
        recent = [job("VM01", now - timedelta(hours=h), kind="AzureIaasVM") for h in (8, 32)]
        ancient = [
            job("VM01", now - timedelta(days=d), kind="AzureIaasVM") for d in range(5, 365)
        ]

        stored = self._vault(session, recent + ancient, monkeypatch)
        session.commit()

        assert stored == 2
        assert session.query(BackupEvent).count() == 2

    def test_a_job_running_over_the_boundary_is_kept(self, session, monkeypatch):
        """The window is on the end time, because that is what decides which
        night a run belongs to. A job that started before the window and
        finished inside it is last night's backup."""
        now = utcnow()
        started = now - timedelta(hours=96) - timedelta(minutes=30)

        stored = self._vault(
            session,
            [job("VM01", started, kind="AzureIaasVM", minutes=90)],
            monkeypatch,
        )
        session.commit()

        assert stored == 1

    def test_the_realistic_estate_does_not_explode(self, session, monkeypatch):
        """Sixty entities, four nights, plus every log backup in between —
        the shape that produced 30,000 records."""
        now = utcnow()
        jobs = []
        for n in range(60):
            for night in range(4):
                jobs.append(
                    job(f"SRV{n:02d}", now - timedelta(days=night, hours=6), backup="Full")
                )
                jobs += [
                    job(
                        f"SRV{n:02d}",
                        now - timedelta(days=night, minutes=15 * q),
                        backup="Log",
                        minutes=1,
                    )
                    for q in range(1, 97)
                ]

        assert len(jobs) > 23_000, "the volume this is about"

        stored = self._vault(session, jobs, monkeypatch)
        session.commit()

        assert stored == 240, "60 servers x 4 nights"
        assert session.query(Server).count() == 60, (
            "and 60 servers, not one per database per log backup"
        )


class TestStoppingOnceThePastIsReached:
    """With the filter ignored, the chain behind a busy vault is years long.
    Reading all of it to discard 99% is the difference between a collection
    that takes seconds and one that takes many minutes."""

    def _run(self, session, jobs, monkeypatch, page_size):
        pages_read = {"n": 0}

        def fake_pages(self, client, url, label=""):
            for start in range(0, len(jobs), page_size):
                pages_read["n"] += 1
                yield jobs[start : start + page_size]

        monkeypatch.setattr(AzureCollector, "_iter_pages", fake_pages)
        now = utcnow()
        stored = AzureCollector()._collect_vault(
            session,
            httpx.Client(),
            ServerCache(session, "azure"),
            "sub",
            "rg",
            "vault",
            now - timedelta(hours=96),
            now,
            )
        session.commit()
        return stored, pages_read["n"]

    def test_it_stops_walking_history_it_has_left_behind(self, session, monkeypatch):
        now = utcnow()
        recent = [job("VM01", now - timedelta(hours=h), kind="AzureIaasVM") for h in (2, 8)]
        ancient = [
            job("VM01", now - timedelta(days=5 + d), kind="AzureIaasVM") for d in range(2000)
        ]

        stored, pages = self._run(session, recent + ancient, monkeypatch, page_size=10)

        assert stored == 2
        assert pages <= 25, f"should have given up early, read {pages} pages"

    def test_oldest_first_is_read_in_full_rather_than_cut_short(self, session, monkeypatch):
        """The stop rule only applies once the window has been seen. If a vault
        ever returns oldest-first, this reads the whole chain — slow, but it
        does not silently lose last night."""
        now = utcnow()
        ancient = [
            job("VM01", now - timedelta(days=5 + d), kind="AzureIaasVM") for d in range(500)
        ]
        recent = [job("VM01", now - timedelta(hours=h), kind="AzureIaasVM") for h in (2, 8)]

        stored, pages = self._run(session, ancient + recent, monkeypatch, page_size=10)

        assert stored == 2, "the recent jobs at the very end must still be found"
        assert pages == 51


def test_the_default_is_to_skip_log_backups():
    """Anyone who wants them can have them; nobody should get 23,000 rows they
    did not ask for on a first run."""
    assert get_settings().azure_include_log_backups is False
