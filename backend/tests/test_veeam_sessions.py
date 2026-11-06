"""A machine added to Veeam has to show up.

The collector knows a machine only by name, and the only place the VBR REST API
names one is a session's log — so the log's shape is the whole question. Three
things about it are not what the PowerShell this replaces saw, and getting any
of them wrong does not error; it makes the log read as empty, files every
session under its *job* name, and a machine that shares a job with others
simply never exists:

  * `GET /api/v1/sessions/{id}/logs` answers `{"totalRecords", "records"}`,
    not `{"data"}` like the list endpoints.
  * A record is `{id, status, startTime, updateTime, title, description}`.
    There is no `message`.
  * The per-machine line is a title reading exactly `Processing winsrv100` —
    no quotes, no "VM" or "computer".

The fixtures here are the API reference's own example log, verbatim.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from functools import partial

import httpx
import pytest

from app.collectors import veeam
from app.collectors.veeam import (
    VeeamCollector,
    extract_names,
    fetch_session_log,
    name_from_text,
    session_machines,
    skip_reason,
    survey_sessions,
)
from app.config import Settings
from app.ingest import ServerCache
from app.models import BackupEvent, Server
from app.timeframes import utcnow

# GET /api/v1/sessions/{id}/logs, from the reference's SessionLogResultExample.
EXAMPLE_LOG = [
    {"status": "Warning", "id": 11, "title": "Job finished with warning at 11/5/2021 6:03:15 AM ", "description": ""},
    {"status": "Succeeded", "id": 10, "title": "Primary bottleneck: Source", "description": ""},
    {"status": "Succeeded", "id": 9, "title": "Load: Source 86% > Proxy 54% > Network 56% > Target 42%", "description": ""},
    {"status": "Succeeded", "id": 7, "title": "Processing ubuntu88", "description": ""},
    {"status": "Succeeded", "id": 5, "title": "Processing winsrv100", "description": ""},
    {"status": "Succeeded", "id": 6, "title": "Processing dbserver01", "description": ""},
    {"status": "Succeeded", "id": 8, "title": "All VMs have been queued for processing", "description": ""},
    {"status": "Succeeded", "id": 4, "title": "Changed block tracking is enabled", "description": ""},
    {"status": "Succeeded", "id": 3, "title": "VM size: 86 GB (48 GB used)", "description": ""},
    {"status": "Succeeded", "id": 2, "title": "Building list of machines to process", "description": ""},
    {"status": "Succeeded", "id": 1, "title": "Job started at 11/5/2021 6:00:02 AM", "description": ""},
]


class TestNamesFromTheRealLogShape:
    def test_the_reference_example_names_its_three_machines_and_nothing_else(self):
        assert extract_names(EXAMPLE_LOG, "Backup Job 1") == ["ubuntu88", "winsrv100", "dbserver01"]

    @pytest.mark.parametrize(
        "title",
        [
            "Job started at 11/5/2021 6:00:02 AM",
            "Building list of machines to process",
            "VM size: 86 GB (48 GB used)",
            "Changed block tracking is enabled",
            "All VMs have been queued for processing",
            "Load: Source 86% > Proxy 54% > Network 56% > Target 42%",
            "Primary bottleneck: Source",
            "Job finished with warning at 11/5/2021 6:03:15 AM ",
            "Processing finished at 11:03 PM",
            "Processing of 3 VMs completed",
        ],
    )
    def test_lines_about_the_job_are_not_machines(self, title):
        assert name_from_text(title) is None

    def test_a_reason_on_the_same_line_is_not_part_of_the_name(self):
        assert name_from_text("Processing sql01 Error: Failed to create snapshot") == "sql01"

    def test_a_fqdn_is_taken_whole_and_normalized_later(self):
        assert name_from_text("Processing sql01.lbfosterco.com") == "sql01.lbfosterco.com"

    def test_a_name_with_spaces_survives(self):
        """vSphere VM names can have spaces; the title is the name and nothing else."""
        assert name_from_text("Processing Web Server 01") == "Web Server 01"

    def test_the_message_field_is_still_honoured(self):
        """The Enterprise Manager shape, and every fixture written before the
        real one was known."""
        items = [{"message": "Processing VM 'APP01'"}, {"message": "Processing 'APP02' failed"}]
        assert extract_names(items, None) == ["APP01"]

    def test_a_description_is_read_strictly(self):
        """An error's free text can contain `Object '...'`; that is not a server."""
        items = [
            {"title": "Error", "description": "Failed to open Object 'C:\\Windows\\Temp\\x.tmp'"},
            {"title": "Retrying", "description": "Processing VM 'APP03' on the next attempt"},
        ]
        assert extract_names(items, None) == ["APP03"]

    def test_the_job_name_is_only_the_last_resort(self):
        names, via = session_machines(EXAMPLE_LOG, "SQL01 Backup")
        assert (names, via) == (["ubuntu88", "winsrv100", "dbserver01"], "log")
        names, via = session_machines([], "SQL01 Backup")
        assert (names, via) == (["SQL01"], "job")
        names, via = session_machines([], "Nightly Tier 1")
        assert (names, via) == ([], "none")


class TestWhichSessionsAreRead:
    """`sessionType` values from the API's ESessionType enum."""

    @pytest.mark.parametrize("kind", ["BackupJob", "AgentBackup", "EndpointBackup", "BackupCopyJob"])
    def test_backup_sessions_are_kept(self, kind):
        assert skip_reason({"sessionType": kind, "endTime": "2026-09-01T23:00:00Z", "name": "Job"}) is None

    @pytest.mark.parametrize("kind", ["ReplicaJob", "RestoreVm", "Infrastructure", "MalwareDetection"])
    def test_other_session_types_are_not(self, kind):
        assert skip_reason({"sessionType": kind, "endTime": "2026-09-01T23:00:00Z", "name": "Job"})

    def test_the_configuration_backup_is_not_a_machine(self):
        row = {"sessionType": "ConfigurationBackup", "endTime": "2026-09-01T23:00:00Z", "name": "Configuration Database Backup"}
        assert skip_reason(row) == "configuration backup"

    def test_a_running_session_waits(self):
        assert skip_reason({"sessionType": "BackupJob", "endTime": None, "name": "Job"}) == "still running"


# --- A fake VBR host ---------------------------------------------------------


def _iso(dt) -> str:
    return dt.replace(microsecond=0).isoformat() + "+00:00"


def make_session(name: str, end, *, kind="BackupJob", result="Success", minutes=45, sid=None) -> dict:
    """A row of GET /api/v1/sessions, shaped like the reference's example."""
    start = end - timedelta(minutes=minutes)
    return {
        "sessionType": kind,
        "state": "Stopped",
        "id": sid or f"{name}-{end:%Y%m%d%H%M%S}",
        "name": name,
        "jobId": "c05dfa57-f59a-4e90-8065-b7f5d3276406",
        "creationTime": _iso(start),
        "endTime": _iso(end),
        "progressPercent": 100,
        "result": {"result": result, "message": "", "isCanceled": False},
    }


def processing(*machines: str) -> list[dict]:
    """A job session's log: the machine lines wrapped in the job lines."""
    body = [{"status": "Succeeded", "id": i + 3, "title": f"Processing {m}", "description": ""} for i, m in enumerate(machines)]
    return (
        [{"status": "Succeeded", "id": 1, "title": "Job started at 9/1/2026 11:00:02 PM", "description": ""},
         {"status": "Succeeded", "id": 2, "title": "Building list of machines to process", "description": ""}]
        + body
        + [{"status": "Succeeded", "id": 99, "title": "Job finished at 9/1/2026 11:45:00 PM", "description": ""}]
    )


def fake_vbr(sessions: list[dict], logs: dict[str, list[dict]], page_size: int = 500) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/oauth2/token":
            return httpx.Response(200, json={"access_token": "t0k3n", "token_type": "bearer"})
        assert request.headers.get("Authorization") == "Bearer t0k3n"
        if path == "/api/v1/sessions":
            skip = int(request.url.params.get("skip", 0))
            page = sessions[skip : skip + page_size]
            return httpx.Response(200, json={"data": page, "pagination": {"total": len(sessions), "count": len(page), "skip": skip, "limit": page_size}})
        match = re.fullmatch(r"/api/v1/sessions/([^/]+)/logs", path)
        if match:
            # The real shape: records, not data; the whole log at once.
            records = logs.get(match.group(1), [])
            return httpx.Response(200, json={"totalRecords": len(records), "records": records})
        return httpx.Response(404, json={"message": path})

    return httpx.MockTransport(handler)


def settings_for(**overrides) -> Settings:
    return Settings(
        veeam_servers="vbr01.example.test",
        veeam_username="svc",
        veeam_password="pw",
        veeam_lookback_hours=96,
        **overrides,
    )


@pytest.fixture
def last_night():
    return utcnow().replace(microsecond=0) - timedelta(hours=9)


def run_host(session, monkeypatch, sessions, logs, caplog=None):
    monkeypatch.setattr(veeam, "open_client", partial(veeam.open_client, transport=fake_vbr(sessions, logs)))
    settings = settings_for()
    collector = VeeamCollector()
    cache = ServerCache(session, "veeam")
    since = utcnow() - timedelta(hours=settings.veeam_lookback_hours)
    written = collector._collect_host(session, settings, cache, "vbr01.example.test", since)
    session.commit()
    return written


class TestTheLogEndpoint:
    def test_records_is_the_key(self):
        transport = fake_vbr([], {"s1": EXAMPLE_LOG})
        with httpx.Client(base_url="https://vbr01", transport=transport, headers={"Authorization": "Bearer t0k3n"}) as client:
            assert fetch_session_log(client, "s1") == EXAMPLE_LOG

    def test_the_older_data_key_still_reads(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": EXAMPLE_LOG[:1]})

        with httpx.Client(base_url="https://vbr01", transport=httpx.MockTransport(handler)) as client:
            assert fetch_session_log(client, "s1") == EXAMPLE_LOG[:1]


class TestAMachineAddedToAnExistingJob:
    """The report that started this: a server added to Veeam a few weeks ago,
    protected every night, absent from the dashboard. Its job protects three
    other machines too."""

    def test_every_machine_in_the_job_gets_an_event(self, session, monkeypatch, last_night):
        row = make_session("Nightly Tier 1", last_night)
        run_host(session, monkeypatch, [row], {row["id"]: processing("APP01", "APP02", "SQL01", "NEWSRV01")})

        names = {s.name for s in session.query(Server).all()}
        assert names == {"APP01", "APP02", "SQL01", "NEWSRV01"}
        assert "NIGHTLY" not in names and "NIGHTLY TIER 1" not in names
        events = session.query(BackupEvent).all()
        assert len(events) == 4
        assert {e.outcome for e in events} == {"success"}
        assert {e.job_name for e in events} == {"Nightly Tier 1"}

    def test_the_session_verdict_reaches_each_machine(self, session, monkeypatch, last_night):
        row = make_session("Nightly Tier 1", last_night, result="Failed")
        run_host(session, monkeypatch, [row], {row["id"]: processing("APP01", "NEWSRV01")})
        assert {e.outcome for e in session.query(BackupEvent).all()} == {"failed"}

    def test_a_single_machine_job_is_filed_under_the_machine_not_the_job(self, session, monkeypatch, last_night):
        row = make_session("Weekly - finance box", last_night)
        run_host(session, monkeypatch, [row], {row["id"]: processing("fin01.lbfosterco.com")})
        assert [s.name for s in session.query(Server).all()] == ["FIN01"]

    def test_non_backup_sessions_in_the_same_window_are_ignored(self, session, monkeypatch, last_night):
        backup = make_session("Nightly Tier 1", last_night)
        replica = make_session("Replica to DR", last_night - timedelta(hours=1), kind="ReplicaJob")
        config = make_session("Configuration Database Backup", last_night - timedelta(hours=2), kind="ConfigurationBackup")
        logs = {
            backup["id"]: processing("APP01"),
            replica["id"]: processing("APP01", "DRTEST01"),
            config["id"]: processing("vbr01"),
        }
        run_host(session, monkeypatch, [backup, replica, config], logs)
        assert [s.name for s in session.query(Server).all()] == ["APP01"]
        assert session.query(BackupEvent).count() == 1

    def test_sessions_older_than_the_window_end_the_walk(self, session, monkeypatch, last_night):
        recent = make_session("Nightly Tier 1", last_night)
        ancient = make_session("Nightly Tier 1", last_night - timedelta(days=30))
        run_host(session, monkeypatch, [recent, ancient], {recent["id"]: processing("APP01"), ancient["id"]: processing("OLD01")})
        assert [s.name for s in session.query(Server).all()] == ["APP01"]


class TestSilenceIsSaidOutLoud:
    def test_a_log_that_names_nothing_is_reported_with_its_job(self, session, monkeypatch, last_night, caplog):
        multi = make_session("Nightly Tier 1", last_night)  # not a hostname: dropped
        single = make_session("SQL01 Backup", last_night - timedelta(hours=1))  # hostname: filed under it
        logs = {multi["id"]: processing(), single["id"]: processing()}
        with caplog.at_level(logging.WARNING, logger="app.collectors.veeam"):
            run_host(session, monkeypatch, [multi, single], logs)

        text = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
        assert "'Nightly Tier 1' x1" in text and "dropped" in text
        assert "'SQL01 Backup' x1" in text and "filed under the job name" in text
        assert "probe veeam --sessions" in text
        assert [s.name for s in session.query(Server).all()] == ["SQL01"]

    def test_a_healthy_host_logs_no_warning(self, session, monkeypatch, last_night, caplog):
        row = make_session("Nightly Tier 1", last_night)
        with caplog.at_level(logging.INFO, logger="app.collectors.veeam"):
            run_host(session, monkeypatch, [row], {row["id"]: processing("APP01", "APP02")})
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        summary = [r.getMessage() for r in caplog.records if "backup sessions since" in r.getMessage()]
        assert summary and "named 2 machines" in summary[0]


class TestTheSurvey:
    """`probe veeam --sessions --find NAME`: the same walk, stored nowhere,
    printed as evidence."""

    def _survey(self, sessions, logs, find=None):
        return survey_sessions(settings_for(), "vbr01.example.test", 96, find, transport=fake_vbr(sessions, logs))

    def test_it_counts_types_and_names_machines(self, last_night):
        backup = make_session("Nightly Tier 1", last_night)
        replica = make_session("Replica to DR", last_night, kind="ReplicaJob")
        result = self._survey([backup, replica], {backup["id"]: processing("APP01", "NEWSRV01")})
        assert result["types"] == {"BackupJob": {"seen": 1, "kept": 1}, "ReplicaJob": {"seen": 1, "kept": 0}}
        assert result["machines"] == {"APP01", "NEWSRV01"}
        kept = [s for s in result["sessions"] if s["skipped"] is None]
        assert kept[0]["names"] == ["APP01", "NEWSRV01"] and kept[0]["via"] == "log"

    def test_find_shows_the_line_even_in_a_skipped_session(self, last_night):
        replica = make_session("Replica to DR", last_night, kind="ReplicaJob")
        result = self._survey([replica], {replica["id"]: processing("NEWSRV01")}, find="newsrv01")
        assert len(result["hits"]) == 1
        hit = result["hits"][0]
        assert hit["skipped"] == "not a backup session"
        assert hit["field"] == "title" and hit["text"] == "Processing NEWSRV01"

    def test_find_with_no_mention_is_an_empty_answer_not_an_error(self, last_night):
        backup = make_session("Nightly Tier 1", last_night)
        result = self._survey([backup], {backup["id"]: processing("APP01")}, find="NEWSRV01")
        assert result["hits"] == []

    def test_sessions_that_name_nothing_are_tallied(self, last_night):
        multi = make_session("Nightly Tier 1", last_night)
        single = make_session("SQL01 Backup", last_night)
        result = self._survey([multi, single], {multi["id"]: processing(), single["id"]: processing()})
        assert dict(result["dropped"]) == {"Nightly Tier 1": 1}
        assert dict(result["fallback"]) == {"SQL01 Backup": 1}
