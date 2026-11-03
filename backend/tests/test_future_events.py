"""A run cannot finish in the future.

Cove reports whatever a device's clock says. LBF-MW-FS came back with a last
session on 29 July 2027: stored, it sorted to the top of the server page and
became both "last run" and "last good backup" — "0m ago", because an age in
the future clamps to zero.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from app.ingest import FUTURE_TOLERANCE, ServerCache, upsert_event
from app.models import BackupEvent, Server
from app.rollups import refresh_server_summaries
from app.timeframes import utcnow


def _write(session, end, outcome="success"):
    cache = ServerCache(session, "nable")
    server = cache.get("LBF-MW-FS")
    created = upsert_event(
        session,
        cache,
        server=server,
        source="nable",
        start_utc=end - timedelta(minutes=8),
        end_utc=end,
        outcome=outcome,
    )
    session.commit()
    return created, server


def test_a_future_run_is_refused_and_logged(session, caplog):
    with caplog.at_level(logging.WARNING, logger="app.ingest"):
        created, _ = _write(session, utcnow() + timedelta(days=268))

    assert created is False
    assert session.query(BackupEvent).count() == 0
    assert "LBF-MW-FS" in caplog.text and "future" in caplog.text


def test_a_clock_a_few_hours_fast_is_tolerated(session):
    created, _ = _write(session, utcnow() + FUTURE_TOLERANCE - timedelta(hours=1))
    assert created is True


def test_one_already_stored_is_removed_and_stops_being_the_last_run(session):
    real_end = utcnow() - timedelta(hours=20)
    _, server = _write(session, real_end, outcome="failed")
    session.add(
        BackupEvent(
            server_id=server.id,
            source="nable",
            start_utc=utcnow() + timedelta(days=268),
            end_utc=utcnow() + timedelta(days=268),
            outcome="warning",
            report_date="2027-07-29",
        )
    )
    session.commit()

    refresh_server_summaries(session)

    assert session.query(BackupEvent).count() == 1
    server = session.get(Server, server.id)
    assert abs(server.last_event_utc - real_end.replace(microsecond=0)) < timedelta(seconds=1)
    assert server.last_success_utc is None
