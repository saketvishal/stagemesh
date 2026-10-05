from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS = [
    Migration(
        2,
        "migration audit log",
        """
        CREATE TABLE IF NOT EXISTS migration_audit (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at REAL NOT NULL
        );
        """,
    ),
    Migration(
        3,
        "append-only evidence records",
        """
        CREATE TABLE IF NOT EXISTS evidence_new (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            candidate_sha TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        INSERT OR IGNORE INTO evidence_new(id, task_id, candidate_sha, kind, status, payload, created_at)
            SELECT id, task_id, candidate_sha, kind, status, payload, created_at FROM evidence;
        DROP TABLE evidence;
        ALTER TABLE evidence_new RENAME TO evidence;
        """,
    ),
    Migration(
        4,
        "execution actor and result",
        """
        ALTER TABLE executions ADD COLUMN actor TEXT;
        ALTER TABLE executions ADD COLUMN result TEXT;
        """,
    ),
]


def apply_migrations(conn: sqlite3.Connection) -> int:
    current = current_schema_version(conn)
    for migration in MIGRATIONS:
        if migration.version <= current:
            continue
        conn.executescript(migration.sql)
        now = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (migration.version, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO migration_audit(version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, now),
        )
        current = migration.version
    _repair_legacy_task_baselines(conn)
    return current_schema_version(conn)


def current_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    return int(row["version"] or 0)


def _repair_legacy_task_baselines(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_baselines)")}
    if "baseline_sha" in columns or "commit_sha" not in columns:
        return
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_baselines_new (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id),
            baseline_sha TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        INSERT OR IGNORE INTO task_baselines_new(task_id, baseline_sha, created_at)
            SELECT task_id, commit_sha, created_at FROM task_baselines WHERE commit_sha IS NOT NULL;
        DROP TABLE task_baselines;
        ALTER TABLE task_baselines_new RENAME TO task_baselines;
        """
    )
