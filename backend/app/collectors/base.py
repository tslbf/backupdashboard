from __future__ import annotations

import logging
import traceback
from abc import ABC, abstractmethod

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import session_factory
from ..models import CollectorRun
from ..timeframes import utcnow

log = logging.getLogger(__name__)


class Collector(ABC):
    source: str
    display_name: str

    @abstractmethod
    def is_configured(self, settings: Settings) -> bool: ...

    @abstractmethod
    def collect(self, session: Session, settings: Settings) -> int:
        """Pull from the source and upsert into the app DB. Returns record count."""

    def interval_minutes(self, settings: Settings) -> int:
        return getattr(settings, f"{self.source}_interval", 60)


def run_collector(collector: Collector) -> CollectorRun:
    """Execute one collector with its own DB session and a run-log row.

    A source that is down must never take the scheduler or the other two sources
    with it — the failure is recorded on the run row and surfaces on the
    Collectors page, which is also the honest answer to "is this dashboard's data
    current?".
    """
    from ..rollups import after_collection

    settings = get_settings()
    SessionLocal = session_factory()
    with SessionLocal() as session:
        run = CollectorRun(source=collector.source, started_at=utcnow())
        session.add(run)
        session.commit()
        run_id = run.id

    with SessionLocal() as session:
        run = session.get(CollectorRun, run_id)
        try:
            if not collector.is_configured(settings):
                raise RuntimeError(f"{collector.display_name} is not configured (missing settings)")
            count = collector.collect(session, settings)
            run.status = "success"
            run.records = count
            session.commit()
            log.info("collector %s finished: %s records", collector.source, count)
        except Exception as exc:  # noqa: BLE001 — a failing source must not kill the scheduler
            session.rollback()
            run = session.get(CollectorRun, run_id)
            run.status = "error"
            run.message = f"{exc.__class__.__name__}: {exc}"
            session.commit()
            log.error("collector %s failed:\n%s", collector.source, traceback.format_exc())
        finally:
            run.finished_at = utcnow()
            session.commit()

        if run.status == "success":
            try:
                after_collection(session)
            except Exception:  # noqa: BLE001
                log.error("rollup after %s failed:\n%s", collector.source, traceback.format_exc())
        return run
