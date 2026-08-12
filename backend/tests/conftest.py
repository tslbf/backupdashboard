from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Point every test at a throwaway SQLite file before app.config is imported —
# settings are cached with lru_cache, so this has to happen first.
_TMP = Path(tempfile.mkdtemp(prefix="backupdash-tests-"))
os.environ["APP_DB_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DISPLAY_TIMEZONE"] = "America/New_York"


@pytest.fixture
def session():
    from app.db import init_db, session_factory
    from app.models import BackupEvent, CollectorRun, Server, ServerDay

    init_db()
    with session_factory()() as db:
        db.query(ServerDay).delete()
        db.query(BackupEvent).delete()
        db.query(Server).delete()
        db.query(CollectorRun).delete()
        db.commit()
        yield db
