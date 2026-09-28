from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class PostgresUnavailable(RuntimeError):
    pass


POSTGRES_SCHEMA_TABLES = (
    "schema_migrations",
    "tasks",
    "task_dependencies",
    "claims",
    "executions",
    "candidates",
    "evidence",
    "source_cache",
    "objectives",
    "workers",
    "source_events",
    "findings",
    "remediation_attempts",
    "work_packets",
    "audit_events",
    "retry_state",
    "external_evidence",
    "migration_audit",
)


POSTGRES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT,
    project TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    UNIQUE(source, source_id)
);
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY(task_id, depends_on_task_id)
);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    worker_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    lease_expires_at DOUBLE PRECISION NOT NULL,
    active BOOLEAN NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_claim
    ON claims(task_id) WHERE active = TRUE;
CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    claim_id TEXT REFERENCES claims(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    pid INTEGER,
    process_create_time DOUBLE PRECISION,
    boot_id TEXT,
    executable TEXT,
    candidate_sha TEXT,
    started_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    sha TEXT NOT NULL,
    produced_by TEXT NOT NULL,
    durable_handoff BOOLEAN NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    UNIQUE(task_id, sha)
);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    candidate_sha TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    UNIQUE(task_id, candidate_sha, kind, status)
);
CREATE TABLE IF NOT EXISTS source_cache (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    state JSONB NOT NULL,
    status TEXT NOT NULL,
    retry_after DOUBLE PRECISION,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(source, source_id)
);
CREATE TABLE IF NOT EXISTS objectives (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    capabilities JSONB NOT NULL,
    pid INTEGER,
    process_create_time DOUBLE PRECISION,
    boot_id TEXT,
    executable TEXT,
    heartbeat_at DOUBLE PRECISION NOT NULL,
    lease_expires_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS source_events (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    candidate_sha TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS remediation_attempts (
    id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL REFERENCES findings(id),
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS work_packets (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    stage TEXT NOT NULL,
    worker_id TEXT,
    candidate_sha TEXT,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS retry_state (
    key TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL,
    next_attempt_at DOUBLE PRECISION NOT NULL,
    reason TEXT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS external_evidence (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    url TEXT NOT NULL,
    candidate_sha TEXT,
    notes TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_audit (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at DOUBLE PRECISION NOT NULL
);
"""


@dataclass(frozen=True)
class PostgresConnectionInfo:
    dsn: str
    driver: str = "psycopg"


class PostgresStore:
    """Minimal optional PostgreSQL store facade.

    The SQLite Store remains the default runtime. This class gives deployments a
    real psycopg-backed connection path without adding psycopg to the base
    install, keeping local installs no-network and dependency-light.
    """

    name = "postgres"

    def __init__(self, dsn: str):
        self.info = PostgresConnectionInfo(dsn)
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:
            raise PostgresUnavailable("psycopg is required for PostgreSQL storage") from exc
        self._psycopg: Any = psycopg
        self.conn = psycopg.connect(dsn)

    def close(self) -> None:
        self.conn.close()

    def ping(self) -> bool:
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
        return bool(row and row[0] == 1)

    def migrate(self) -> None:
        with self.conn.cursor() as cursor:
            for statement in postgres_schema_statements():
                cursor.execute(statement)
            cursor.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (%s, EXTRACT(EPOCH FROM NOW())) ON CONFLICT (version) DO NOTHING",
                (1,),
            )
            cursor.execute(
                """
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (%s, EXTRACT(EPOCH FROM NOW()))
                ON CONFLICT (version) DO NOTHING
                """,
                (2,),
            )
            cursor.execute(
                """
                INSERT INTO migration_audit(version, name, applied_at)
                VALUES (%s, %s, EXTRACT(EPOCH FROM NOW()))
                ON CONFLICT (version) DO NOTHING
                """,
                (2, "migration audit log"),
            )
        self.conn.commit()


def postgres_available() -> bool:
    try:
        __import__("psycopg")
    except ImportError:
        return False
    return True


def postgres_schema_contract() -> dict[str, object]:
    return {
        "dialect": "postgresql",
        "tables": list(POSTGRES_SCHEMA_TABLES),
        "schema_sql": POSTGRES_SCHEMA_SQL.strip(),
    }


def postgres_schema_statements() -> list[str]:
    return [statement.strip() for statement in POSTGRES_SCHEMA_SQL.split(";") if statement.strip()]
