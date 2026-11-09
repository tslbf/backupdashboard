"""Is each collector actually working?

A collector that breaks does not make the dashboard go red in an obvious way.
Veeam has a real history endpoint, so a Veeam outage shows up as every Veeam
server reading "No backup" — which looks like a backup problem on forty
servers, not a credentials problem on one service account. Cove's outage is
quieter still: `CURRENT_STATE_SOURCES` deliberately stops judging nights nobody
observed, so a dead Cove collector produces *no* red at all.

So collector health is its own verdict, checked in four ways:

- **error**   — the last run raised.
- **partial** — the last run got some data but not all of it (one of two VBR
                hosts down, session logs refused). Recorded as `partial` rather
                than `success`, because "success" with half the estate missing
                is exactly the silent failure this app exists to catch.
- **stale**   — nothing has succeeded for longer than the schedule allows. This
                is what catches the scheduler itself stopping.
- **empty**   — it succeeded and returned zero records. A 96-hour Veeam window
                with no sessions in it is not a quiet weekend; it is a filter,
                a permission or a server-name change.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .models import CollectorRun
from .timeframes import utcnow

FAILING_STATUSES = ("error", "partial")

# A daily run has to have succeeded within this long. 26h, not 24h: the
# morning run itself takes a few minutes, and DST shifts it by an hour.
DAILY_GRACE = timedelta(hours=26)


@dataclass
class CollectorProblem:
    source: str
    display_name: str
    kind: str  # error | partial | stale | empty
    message: str
    since: datetime | None  # when it started failing, if known

    def as_dict(self) -> dict:
        data = asdict(self)
        data["since"] = self.since.isoformat() + "Z" if self.since else None
        return data


def _allowed_gap(collector, settings: Settings) -> timedelta | None:
    """How long this collector may go without a success before that is news.

    None means it has no schedule, so silence is expected (manual-only sources,
    and the legacy backfill)."""
    gaps = []
    if settings.collect_time_parts() is not None and collector.schedulable(settings):
        gaps.append(DAILY_GRACE)
    minutes = collector.interval_minutes(settings)
    if minutes > 0:
        # Two missed polls, not one — a single slow run is not an outage.
        gaps.append(timedelta(minutes=minutes * 2 + 30))
    return min(gaps) if gaps else None


def _first_failure(session: Session, source: str, last_success: CollectorRun | None) -> datetime | None:
    query = session.query(CollectorRun).filter(
        CollectorRun.source == source, CollectorRun.status.in_(FAILING_STATUSES)
    )
    if last_success is not None:
        query = query.filter(CollectorRun.started_at > last_success.started_at)
    first = query.order_by(CollectorRun.started_at.asc()).first()
    return first.started_at if first else None


def collector_problems(session: Session, settings: Settings | None = None) -> list[CollectorProblem]:
    """Every configured, scheduled collector that is not doing its job."""
    from .collectors import ALL_COLLECTORS

    settings = settings or get_settings()
    now = utcnow()
    problems: list[CollectorProblem] = []

    for collector in ALL_COLLECTORS.values():
        if not collector.is_configured(settings):
            continue
        gap = _allowed_gap(collector, settings)
        if gap is None:
            continue  # manual-only: nothing promised, nothing to break

        runs = session.query(CollectorRun).filter(CollectorRun.source == collector.source)
        last = runs.filter(CollectorRun.status != "running").order_by(
            CollectorRun.started_at.desc()
        ).first()
        last_success = runs.filter(CollectorRun.status == "success").order_by(
            CollectorRun.started_at.desc()
        ).first()

        def add(kind: str, message: str, since: datetime | None) -> None:
            problems.append(
                CollectorProblem(collector.source, collector.display_name, kind, message, since)
            )

        if last is None:
            # Configured and scheduled but never run: only news once a run was due.
            continue

        if last.status in FAILING_STATUSES:
            add(
                last.status,
                last.message or "no message recorded",
                _first_failure(session, collector.source, last_success),
            )
        elif last_success is not None and now - last_success.started_at > gap:
            hours = int((now - last_success.started_at).total_seconds() // 3600)
            add(
                "stale",
                f"no successful run for {hours}h — is the app/scheduler running?",
                last_success.started_at,
            )
        elif last.status == "success" and (last.records or 0) == 0:
            add(
                "empty",
                "last run succeeded but returned 0 records — check the account's "
                "permissions and the lookback window",
                last.started_at,
            )
    return problems


def collector_health(session: Session, settings: Settings | None = None) -> list[dict]:
    return [p.as_dict() for p in collector_problems(session, settings)]
