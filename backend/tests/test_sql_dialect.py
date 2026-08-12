"""Queries have to be valid on SQL Server, not just on SQLite.

The whole suite runs on SQLite, which is permissive in ways SQL Server is not.
`Server.hidden.is_(False)` compiles to `hidden IS 0` — fine in SQLite, and a
hard syntax error in T-SQL, whose `IS` accepts only NULL. That shipped, and only
surfaced against a real AZUSCCM01:

    [42000] Incorrect syntax near '0'. (102)
    WHERE server_days.report_date IN (?, ?) AND servers.hidden IS 0

Compiling against the mssql dialect needs no server, so there is no excuse for
finding this the slow way twice.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import mssql, sqlite

from app.models import (
    BackupEvent,
    Server,
    ServerDay,
    expected_servers,
    visible_servers,
)

DIALECTS = {"mssql": mssql.dialect(), "sqlite": sqlite.dialect()}

# Statements shaped like the ones the endpoints build. Compiled, never executed.
STATEMENTS = {
    "overview": select(ServerDay, Server)
    .join(Server, Server.id == ServerDay.server_id)
    .where(ServerDay.report_date.in_(["2026-08-12", "2026-08-11"]), visible_servers()),
    "trends": select(ServerDay.report_date, ServerDay.outcome, func.count(ServerDay.id))
    .join(Server, Server.id == ServerDay.server_id)
    .where(visible_servers(), ServerDay.report_date >= "2026-07-14")
    .group_by(ServerDay.report_date, ServerDay.outcome),
    "duration_trend": select(ServerDay.report_date, ServerDay.duration_sec)
    .join(Server, Server.id == ServerDay.server_id)
    .where(visible_servers(), ServerDay.duration_sec.isnot(None)),
    "servers_grid": select(Server).where(visible_servers()),
    "attention": select(Server).where(
        visible_servers(),
        expected_servers(),
        Server.last_success_utc.is_(None),
        Server.last_event_utc.isnot(None),
    ),
    "day_detail": select(ServerDay, Server)
    .join(Server, Server.id == ServerDay.server_id)
    .where(ServerDay.report_date == "2026-08-12", visible_servers()),
    "events_for_server": select(BackupEvent)
    .where(BackupEvent.server_id == 1)
    .order_by(BackupEvent.end_utc.desc())
    .limit(200),
}

# `IS <literal>` is only ever legal with NULL. Anything else is the bug above.
BAD_IS = re.compile(r"\bIS\s+(?!NULL\b)(?!NOT\s+NULL\b)\S", re.IGNORECASE)


@pytest.mark.parametrize("name", sorted(STATEMENTS))
@pytest.mark.parametrize("dialect", sorted(DIALECTS))
def test_statement_compiles_without_is_literal(name, dialect):
    sql = str(STATEMENTS[name].compile(dialect=DIALECTS[dialect]))
    match = BAD_IS.search(sql)
    assert match is None, (
        f"{name} on {dialect} renders `IS <literal>`, which SQL Server rejects "
        f"(102, incorrect syntax). Use a comparison — see models.visible_servers.\n"
        f"  ...{sql[max(0, match.start() - 60):match.end() + 20]}..."
    )


def test_visible_and_expected_render_as_comparisons_on_sql_server():
    """The specific rendering that broke, pinned."""
    for predicate in (visible_servers(), expected_servers()):
        sql = str(predicate.compile(dialect=mssql.dialect()))
        assert " IS 0" not in sql and " IS 1" not in sql, sql
        assert "IS NULL" in sql, "the NULL branch covers rows predating the column"


def test_no_boolean_is_calls_remain_in_the_app():
    """A source-level guard, because the next one of these will be written by
    hand in a new endpoint rather than caught by the statements above.

    Parsed rather than grepped: the docstrings explaining this bug necessarily
    contain the very text a text search would flag.
    """
    offenders = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "is_"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, bool)
            ):
                offenders.append(f"{path}:{node.lineno}: .is_({node.args[0].value})")
    assert not offenders, (
        "`.is_(True)` / `.is_(False)` compile to `IS 1` / `IS 0`, which SQL Server "
        "rejects. Use models.visible_servers() / expected_servers(), or a plain "
        "`== True` / `== False` comparison:\n  " + "\n  ".join(offenders)
    )
