from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus, Stage, TaskStatus

SCHEMA_VERSION = 1


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                source_id TEXT,
                project TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(source, source_id)
            );
            CREATE TABLE IF NOT EXISTS claims (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                worker_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                lease_expires_at REAL NOT NULL,
                active INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_claim
                ON claims(task_id) WHERE active = 1;
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                claim_id TEXT REFERENCES claims(id),
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                pid INTEGER,
                process_create_time REAL,
                boot_id TEXT,
                executable TEXT,
                candidate_sha TEXT,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidates (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                sha TEXT NOT NULL,
                produced_by TEXT NOT NULL,
                durable_handoff INTEGER NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(task_id, sha)
            );
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                candidate_sha TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(task_id, candidate_sha, kind, status)
            );
            CREATE TABLE IF NOT EXISTS source_cache (
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                state TEXT NOT NULL,
                status TEXT NOT NULL,
                retry_after REAL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(source, source_id)
            );
            CREATE TABLE IF NOT EXISTS objectives (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, time.time()),
        )
        self.conn.commit()

    def upsert_task(self, title: str, source: str = "local", source_id: str | None = None, project: str | None = None) -> str:
        task_id = source_id or str(uuid.uuid4())
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO tasks(id, title, stage, status, source, source_id, project, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at
            """,
            (task_id, title, Stage.PLAN, TaskStatus.OPEN, source, source_id, project, now, now),
        )
        self.conn.commit()
        return task_id

    def tasks(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM tasks ORDER BY created_at"))

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def acquire_claim(self, task_id: str, worker_id: str, lease_seconds: float = 300) -> str | None:
        task = self.get_task(task_id)
        if not task or task["status"] == TaskStatus.DONE or task["stage"] == Stage.DONE:
            return None
        now = time.time()
        with self.conn:
            self.conn.execute(
                "UPDATE claims SET active=0 WHERE task_id=? AND active=1 AND lease_expires_at < ?",
                (task_id, now),
            )
            active = self.conn.execute(
                "SELECT id FROM claims WHERE task_id=? AND active=1", (task_id,)
            ).fetchone()
            if active:
                return None
            claim_id = str(uuid.uuid4())
            self.conn.execute(
                "INSERT INTO claims VALUES (?, ?, ?, ?, ?, 1, ?)",
                (claim_id, task_id, worker_id, task["stage"], now + lease_seconds, now),
            )
            self.conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE id=?", (TaskStatus.CLAIMED, now, task_id)
            )
            return claim_id

    def start_execution(
        self,
        *,
        task_id: str,
        claim_id: str | None,
        kind: ExecutionKind,
        pid: int | None = None,
        process_create_time: float | None = None,
        boot_id: str | None = None,
        executable: str | None = None,
        candidate_sha: str | None = None,
    ) -> str:
        execution_id = str(uuid.uuid4())
        now = time.time()
        self.conn.execute(
            "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                execution_id,
                task_id,
                claim_id,
                kind,
                ExecutionStatus.RUNNING,
                pid,
                process_create_time,
                boot_id,
                executable,
                candidate_sha,
                now,
                now,
            ),
        )
        self.conn.commit()
        return execution_id

    def finish_execution(self, execution_id: str, status: ExecutionStatus, candidate_sha: str | None = None) -> None:
        self.conn.execute(
            "UPDATE executions SET status=?, candidate_sha=COALESCE(?, candidate_sha), updated_at=? WHERE id=?",
            (status, candidate_sha, time.time(), execution_id),
        )
        self.conn.commit()

    def add_candidate(self, task_id: str, sha: str, produced_by: str, durable_handoff: bool) -> str:
        cid = str(uuid.uuid4())
        self.conn.execute(
            "INSERT OR IGNORE INTO candidates VALUES (?, ?, ?, ?, ?, ?)",
            (cid, task_id, sha, produced_by, int(durable_handoff), time.time()),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM candidates WHERE task_id=? AND sha=?", (task_id, sha)).fetchone()
        return str(row["id"])

    def latest_candidate(self, task_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM candidates WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)
        ).fetchone()

    def add_evidence(
        self,
        task_id: str,
        candidate_sha: str,
        kind: EvidenceKind,
        status: EvidenceStatus,
        payload: dict[str, Any] | None = None,
    ) -> str:
        eid = str(uuid.uuid4())
        self.conn.execute(
            "INSERT OR IGNORE INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eid, task_id, candidate_sha, kind, status, json.dumps(payload or {}, sort_keys=True), time.time()),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
            (task_id, candidate_sha, kind, status),
        ).fetchone()
        return str(row["id"])

    def has_evidence(self, task_id: str, sha: str, kind: EvidenceKind, status: EvidenceStatus = EvidenceStatus.PASSED) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
                (task_id, sha, kind, status),
            ).fetchone()
            is not None
        )

    def advance_task(self, task_id: str, stage: Stage) -> None:
        status = TaskStatus.DONE if stage is Stage.DONE else TaskStatus.OPEN
        self.conn.execute(
            "UPDATE tasks SET stage=?, status=?, updated_at=? WHERE id=?", (stage, status, time.time(), task_id)
        )
        self.conn.execute("UPDATE claims SET active=0 WHERE task_id=?", (task_id,))
        self.conn.commit()

    def running_executions(self) -> Iterable[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM executions WHERE status=?", (ExecutionStatus.RUNNING,))

    def cache_source(
        self,
        source: str,
        source_id: str,
        state: dict[str, Any],
        status: str,
        retry_after: float | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO source_cache VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_id) DO UPDATE SET state=excluded.state, status=excluded.status, retry_after=excluded.retry_after, updated_at=excluded.updated_at
            """,
            (source, source_id, json.dumps(state, sort_keys=True), status, retry_after, time.time()),
        )
        self.conn.commit()

    def save_objective(self, objective_id: str, title: str, payload: dict[str, Any]) -> None:
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO objectives VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title, payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (objective_id, title, json.dumps(payload, sort_keys=True), now, now),
        )
        self.conn.commit()
