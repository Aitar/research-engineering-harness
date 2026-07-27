from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

HARNESS_DIR = ".harness"
DB_NAME = "harness.db"


def database_path(root: Path) -> Path:
    return root / HARNESS_DIR / DB_NAME


def make_engine(root: Path) -> Engine:
    db_path = database_path(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def init_database(root: Path) -> None:
    engine = make_engine(root)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


@contextmanager
def session_scope(root: Path, *, write: bool = True) -> Iterator[Session]:
    engine = make_engine(root)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        if write:
            # Serialize writers before they read sequence/version counters. SQLite's
            # default deferred transactions otherwise allow concurrent agents to
            # choose the same next value and fail with a uniqueness error.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        yield session
        session.commit()
    except Exception:
        session.rollback()
        for path in reversed(session.info.get("rollback_files", [])):
            try:
                Path(path).unlink(missing_ok=True)
                parent = Path(path).parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass
        raise
    else:
        # Derived views are intentionally refreshed only after authoritative state
        # commits. Callback failures must never roll back or delete committed evidence.
        for callback in session.info.get("after_commit", []):
            callback()
    finally:
        session.close()
        engine.dispose()
