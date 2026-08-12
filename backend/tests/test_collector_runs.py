"""Run bookkeeping and the live log.

`run_collector` writes status="running" before it starts and overwrites it at
the end. Nothing rewrites it if the process dies in between, so a Ctrl+C mid
collection leaves a row claiming to be running forever — and the Collectors page
believed it.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from app.collectors import close_orphaned_runs
from app.logbuffer import RingBufferHandler
from app.models import CollectorRun
from app.timeframes import utcnow


class TestOrphanedRuns:
    def test_a_run_left_running_is_closed_on_startup(self, session):
        session.add(
            CollectorRun(
                source="veeam",
                started_at=utcnow() - timedelta(hours=3),
                status="running",
            )
        )
        session.commit()

        assert close_orphaned_runs() == 1

        run = session.query(CollectorRun).one()
        assert run.status == "error"
        assert run.finished_at is not None
        assert "Interrupted" in run.message

    def test_finished_runs_are_untouched(self, session):
        session.add(
            CollectorRun(
                source="azure",
                started_at=utcnow() - timedelta(hours=2),
                finished_at=utcnow() - timedelta(hours=2),
                status="success",
                records=41,
            )
        )
        session.commit()

        assert close_orphaned_runs() == 0
        assert session.query(CollectorRun).one().status == "success"

    def test_it_is_safe_to_run_twice(self, session):
        session.add(CollectorRun(source="nable", started_at=utcnow(), status="running"))
        session.commit()

        assert close_orphaned_runs() == 1
        assert close_orphaned_runs() == 0


class TestLogBuffer:
    def _record(self, message: str, level: int = logging.INFO, name: str = "app.collectors.veeam"):
        return logging.LogRecord(name, level, "x.py", 1, message, None, None)

    def test_entries_after_an_id_returns_only_newer_lines(self):
        handler = RingBufferHandler(capacity=50)
        for i in range(5):
            handler.emit(self._record(f"line {i}"))

        everything = handler.entries()
        assert [e["message"] for e in everything] == [f"line {i}" for i in range(5)]

        # what the UI does on each poll
        fresh = handler.entries(after=everything[2]["id"])
        assert [e["message"] for e in fresh] == ["line 3", "line 4"]

    def test_the_buffer_is_bounded(self):
        """It runs for months on a server; it cannot grow without limit."""
        handler = RingBufferHandler(capacity=10)
        for i in range(50):
            handler.emit(self._record(f"line {i}"))

        entries = handler.entries(limit=100)
        assert len(entries) == 10
        assert entries[0]["message"] == "line 40"
        # ids keep climbing even as old lines fall off, so `after` stays correct
        assert entries[-1]["id"] == 50

    def test_request_noise_is_dropped(self):
        """Per-request lines would bury the collector output that the panel is
        actually for."""
        handler = RingBufferHandler()
        handler.emit(self._record("GET /api/overview", name="uvicorn.access"))
        handler.emit(self._record("veeam: starting collection"))

        assert [e["message"] for e in handler.entries()] == ["veeam: starting collection"]

    def test_level_and_logger_are_carried_through(self):
        handler = RingBufferHandler()
        handler.emit(self._record("boom", level=logging.ERROR))

        entry = handler.entries()[0]
        assert entry["level"] == "ERROR"
        assert entry["logger"] == "app.collectors.veeam"
        assert entry["ts"]

    def test_a_broken_record_does_not_raise_into_the_app(self):
        """Logging must never take down the thing it is logging about."""
        handler = RingBufferHandler()
        record = logging.LogRecord("app", logging.INFO, "x.py", 1, "%d", ("not a number",), None)
        handler.emit(record)  # would raise inside getMessage without the guard
        assert len(handler.entries()) == 1
