from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().app_db_url
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
    return _engine


def session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


def get_session():
    """FastAPI dependency."""
    session: Session = session_factory()()
    try:
        yield session
    finally:
        session.close()


def _ensure_columns(engine) -> None:
    """Additive micro-migrations: add columns that create_all won't add to
    existing tables. `ADD <col>` (no COLUMN keyword) parses on SQLite and SQL
    Server alike."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    added = {
        "servers": {
            "primary_source": "VARCHAR(16)",
            "timezone": "VARCHAR(64)",
            "night_cutoff_hour": "INTEGER",
            "expected": "BOOLEAN",
            "hidden": "BOOLEAN",
            "notes": "TEXT",
            "last_event_utc": "DATETIME",
            "last_success_utc": "DATETIME",
        },
        "backup_events": {
            "bytes_transferred": "BIGINT",
            "native_id": "VARCHAR(128)",
            "details": "TEXT",
        },
        "source_config": {
            "default_timezone": "VARCHAR(64)",
        },
    }
    for table, columns in added.items():
        if table not in inspector.get_table_names():
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl_type in columns.items():
            if name in existing:
                continue
            if ddl_type == "BOOLEAN" and engine.dialect.name == "mssql":
                ddl_type = "BIT"
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD {name} {ddl_type}"))


def init_db() -> None:
    from . import models

    engine = get_engine()
    models.Base.metadata.create_all(engine)
    _ensure_columns(engine)
    with session_factory()() as session:
        models.seed_source_config(session)
        session.commit()
