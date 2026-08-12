"""Deleting things, with foreign keys actually enforced.

SQLite ignores foreign keys unless asked; SQL Server never does. Now that the
app targets SQL Server these tests turn enforcement on, so a delete order that
only works because SQLite is permissive fails here instead of in production.

What keeps `purge` safe today is not the delete order but an invariant one step
removed: every server_day carries a source, including the synthetic `missed`
rows that have no event behind them. So purging a source takes its server_days
with it, and a server can only be orphaned once every one of its sources is
gone. That is load-bearing and entirely implicit — hence these tests.
"""
from __future__ import annotations

import pytest
from sqlalchemy import event

from app.cli import _purge
from app.db import get_engine
from app.models import BackupEvent, Server, ServerDay
from app.rollups import refresh_days
from test_rollups import DATES, add_event

from app.outcomes import MISSED, SUCCESS


@pytest.fixture(autouse=True)
def enforce_foreign_keys():
    engine = get_engine()

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    # Pooled connections predate the listener, so they would not have the
    # pragma set; without this the fixture silently enforces nothing.
    engine.dispose()
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1, (
            "foreign keys are not being enforced — this test would pass vacuously"
        )
    yield
    event.remove(engine, "connect", _fk_on)
    engine.dispose()


def test_purging_a_source_leaves_no_dangling_server_days(session):
    """Purge deletes servers that have no events left. Those servers must have
    no server_days left either, or the foreign key rejects the delete.

    It holds because the `missed` rows — the ones with no event behind them —
    are still stamped with the source that was expected to run, so the
    source-filtered delete catches them. Break that stamping and this fails."""
    for day in DATES[:4]:
        add_event(session, "GONE01", day, SUCCESS, source="veeam")
    refresh_days(session, DATES)

    server_id = session.query(Server).filter(Server.name == "GONE01").one().id
    assert session.query(ServerDay).filter(
        ServerDay.server_id == server_id, ServerDay.outcome == MISSED
    ).count() > 0, "the fixture needs at least one event-less missed row to be meaningful"

    _purge("veeam")

    assert session.query(BackupEvent).count() == 0
    assert session.query(Server).filter(Server.id == server_id).count() == 0
    assert session.query(ServerDay).filter(ServerDay.server_id == server_id).count() == 0


def test_purging_keeps_servers_that_still_have_other_events(session):
    """A machine backed up by two tools survives losing one of them."""
    add_event(session, "KEEP01", DATES[0], SUCCESS, source="veeam")
    add_event(session, "KEEP01", DATES[0], SUCCESS, source="nable", hour=22)
    refresh_days(session, DATES[:1])

    _purge("veeam")

    assert session.query(Server).filter(Server.name == "KEEP01").count() == 1
    assert session.query(BackupEvent).count() == 1
