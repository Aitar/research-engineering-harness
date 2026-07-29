from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

LATEST_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[Connection], None]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _table_exists(connection: Connection, name: str) -> bool:
    return (
        connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).scalar_one_or_none()
        is not None
    )


def _migration_2_operational_hardening(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS idempotency_requests (
            project_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            request_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('in_progress', 'completed', 'failed')),
            entity_type TEXT,
            entity_id TEXT,
            response_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(project_id, operation, request_key),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS ix_idempotency_status
        ON idempotency_requests(project_id, status, updated_at)
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS ci_trust_roots (
            project_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            key_id TEXT NOT NULL,
            algorithm TEXT NOT NULL CHECK(algorithm = 'ed25519'),
            public_key_pem TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(project_id, provider, key_id),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )


MIGRATIONS = (
    Migration(2, "operational-hardening", _migration_2_operational_hardening),
)


def upgrade_engine(engine: Engine) -> list[int]:
    """Upgrade a Harness SQLite database and return the applied versions."""
    applied: list[int] = []
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        current = int(connection.exec_driver_sql("PRAGMA user_version").scalar_one())
        if current == 0:
            # Databases created before the migration framework are the v1 baseline.
            # Fresh initialization also creates the domain tables before this function runs.
            if _table_exists(connection, "projects"):
                current = 1
                connection.execute(
                    text(
                        """
                        INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
                        VALUES (1, 'baseline-domain-schema', :applied_at)
                        """
                    ),
                    {"applied_at": _utc_now()},
                )
                connection.exec_driver_sql("PRAGMA user_version = 1")
        if current > LATEST_SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current} is newer than supported "
                f"version {LATEST_SCHEMA_VERSION}."
            )
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            migration.apply(connection)
            connection.execute(
                text(
                    """
                    INSERT INTO schema_migrations(version, name, applied_at)
                    VALUES (:version, :name, :applied_at)
                    """
                ),
                {
                    "version": migration.version,
                    "name": migration.name,
                    "applied_at": _utc_now(),
                },
            )
            connection.exec_driver_sql(f"PRAGMA user_version = {migration.version}")
            current = migration.version
            applied.append(migration.version)
    return applied


def schema_status(engine: Engine) -> dict[str, object]:
    with engine.connect() as connection:
        current = int(connection.exec_driver_sql("PRAGMA user_version").scalar_one())
        rows = []
        if _table_exists(connection, "schema_migrations"):
            rows = [
                dict(row._mapping)
                for row in connection.execute(
                    text(
                        "SELECT version, name, applied_at "
                        "FROM schema_migrations ORDER BY version"
                    )
                )
            ]
    return {
        "current_version": current,
        "latest_version": LATEST_SCHEMA_VERSION,
        "pending": max(0, LATEST_SCHEMA_VERSION - current),
        "applied": rows,
    }
