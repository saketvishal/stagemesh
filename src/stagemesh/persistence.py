from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus, Stage, TaskStatus
from .migrations import apply_migrations, current_schema_version

SCHEMA_VERSION = 2


class StoreValidationError(ValueError):
    pass


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")

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
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                capabilities TEXT NOT NULL,
                pid INTEGER,
                process_create_time REAL,
                boot_id TEXT,
                executable TEXT,
                heartbeat_at REAL NOT NULL,
                lease_expires_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_events (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS findings (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                candidate_sha TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS remediation_attempts (
                id TEXT PRIMARY KEY,
                finding_id TEXT NOT NULL REFERENCES findings(id),
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_packets (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                stage TEXT NOT NULL,
                worker_id TEXT,
                candidate_sha TEXT,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS retry_state (
                key TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL,
                next_attempt_at REAL NOT NULL,
                reason TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS external_evidence (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                url TEXT NOT NULL,
                candidate_sha TEXT,
                notes TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (1, time.time()),
        )
        apply_migrations(self.conn)
        self.conn.commit()

    def schema_version(self) -> int:
        return current_schema_version(self.conn)

    def upsert_task(self, title: str, source: str = "local", source_id: str | None = None, project: str | None = None) -> str:
        title = _validate_text(title, "task title")
        source = _validate_text(source, "task source")
        source_id = _validate_optional_text(source_id, "task source id")
        project = _validate_optional_text(project, "task project")
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

    def add_dependency(self, task_id: str, depends_on_task_id: str) -> None:
        task_id = _validate_text(task_id, "task id")
        depends_on_task_id = _validate_text(depends_on_task_id, "dependency task id")
        self.conn.execute(
            "INSERT OR IGNORE INTO task_dependencies VALUES (?, ?)",
            (task_id, depends_on_task_id),
        )
        self.conn.commit()

    def incomplete_dependencies(self, task_id: str) -> list[str]:
        return [
            str(row["depends_on_task_id"])
            for row in self.conn.execute(
                """
                SELECT d.depends_on_task_id
                FROM task_dependencies d
                JOIN tasks t ON t.id = d.depends_on_task_id
                WHERE d.task_id=? AND t.stage != ?
                ORDER BY d.depends_on_task_id
                """,
                (task_id, Stage.DONE),
            )
        ]

    def tasks(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM tasks ORDER BY created_at"))

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        task_id = _validate_text(task_id, "task id")
        return self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def acquire_claim(self, task_id: str, worker_id: str, lease_seconds: float = 300) -> str | None:
        task_id = _validate_text(task_id, "task id")
        worker_id = _validate_text(worker_id, "worker id")
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
        task_id = _validate_text(task_id, "task id")
        claim_id = _validate_optional_text(claim_id, "claim id")
        kind = _validate_enum(kind, ExecutionKind, "execution kind")
        candidate_sha = _validate_optional_text(candidate_sha, "candidate sha")
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
        execution_id = _validate_text(execution_id, "execution id")
        status = _validate_enum(status, ExecutionStatus, "execution status")
        candidate_sha = _validate_optional_text(candidate_sha, "candidate sha")
        self.conn.execute(
            "UPDATE executions SET status=?, candidate_sha=COALESCE(?, candidate_sha), updated_at=? WHERE id=?",
            (status, candidate_sha, time.time(), execution_id),
        )
        self.conn.commit()

    def add_candidate(self, task_id: str, sha: str, produced_by: str, durable_handoff: bool) -> str:
        task_id = _validate_text(task_id, "task id")
        sha = _validate_text(sha, "candidate sha")
        produced_by = _validate_text(produced_by, "candidate producer")
        if not isinstance(durable_handoff, bool):
            raise StoreValidationError("durable_handoff must be a boolean")
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
        task_id = _validate_text(task_id, "task id")
        candidate_sha = _validate_text(candidate_sha, "candidate sha")
        kind = _validate_enum(kind, EvidenceKind, "evidence kind")
        status = _validate_enum(status, EvidenceStatus, "evidence status")
        payload = _validate_payload(payload)
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
        task_id = _validate_text(task_id, "task id")
        sha = _validate_text(sha, "candidate sha")
        kind = _validate_enum(kind, EvidenceKind, "evidence kind")
        status = _validate_enum(status, EvidenceStatus, "evidence status")
        return (
            self.conn.execute(
                "SELECT 1 FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
                (task_id, sha, kind, status),
            ).fetchone()
            is not None
        )

    def advance_task(self, task_id: str, stage: Stage) -> None:
        task_id = _validate_text(task_id, "task id")
        stage = _validate_enum(stage, Stage, "task stage")
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
        source = _validate_text(source, "source")
        source_id = _validate_text(source_id, "source id")
        state = _validate_payload(state)
        status = _validate_text(status, "source status")
        self.conn.execute(
            """
            INSERT INTO source_cache VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_id) DO UPDATE SET state=excluded.state, status=excluded.status, retry_after=excluded.retry_after, updated_at=excluded.updated_at
            """,
            (source, source_id, json.dumps(state, sort_keys=True), status, retry_after, time.time()),
        )
        self.conn.commit()

    def save_objective(self, objective_id: str, title: str, payload: dict[str, Any]) -> None:
        objective_id = _validate_text(objective_id, "objective id")
        title = _validate_text(title, "objective title")
        payload = _validate_payload(payload)
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO objectives VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title, payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (objective_id, title, json.dumps(payload, sort_keys=True), now, now),
        )
        self.conn.commit()

    def upsert_worker(
        self,
        *,
        worker_id: str,
        provider: str,
        capabilities: list[str],
        pid: int | None,
        process_create_time: float | None,
        boot_id: str | None,
        executable: str | None,
        heartbeat_at: float,
        lease_expires_at: float,
    ) -> None:
        worker_id = _validate_text(worker_id, "worker id")
        provider = _validate_text(provider, "worker provider")
        if not isinstance(capabilities, list) or not capabilities:
            raise StoreValidationError("worker capabilities must be a non-empty list")
        capabilities = [_validate_text(capability, "worker capability") for capability in capabilities]
        if len(set(capabilities)) != len(capabilities):
            raise StoreValidationError("worker capabilities must be unique")
        boot_id = _validate_optional_text(boot_id, "worker boot id")
        executable = _validate_optional_text(executable, "worker executable", 1000)
        self.conn.execute(
            """
            INSERT INTO workers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                provider=excluded.provider,
                capabilities=excluded.capabilities,
                pid=excluded.pid,
                process_create_time=excluded.process_create_time,
                boot_id=excluded.boot_id,
                executable=excluded.executable,
                heartbeat_at=excluded.heartbeat_at,
                lease_expires_at=excluded.lease_expires_at,
                updated_at=excluded.updated_at
            """,
            (
                worker_id,
                provider,
                json.dumps(capabilities),
                pid,
                process_create_time,
                boot_id,
                executable,
                heartbeat_at,
                lease_expires_at,
                time.time(),
            ),
        )
        self.conn.commit()

    def heartbeat_worker(self, worker_id: str, heartbeat_at: float, lease_expires_at: float) -> None:
        worker_id = _validate_text(worker_id, "worker id")
        self.conn.execute(
            "UPDATE workers SET heartbeat_at=?, lease_expires_at=?, updated_at=? WHERE id=?",
            (heartbeat_at, lease_expires_at, time.time(), worker_id),
        )
        self.conn.commit()

    def workers(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM workers ORDER BY id"))

    def add_source_event(
        self,
        source: str,
        source_id: str,
        direction: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        source = _validate_text(source, "source")
        source_id = _validate_text(source_id, "source id")
        direction = _validate_text(direction, "source event direction")
        status = _validate_text(status, "source event status")
        payload = _validate_payload(payload)
        event_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO source_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                source,
                source_id,
                direction,
                status,
                json.dumps(payload, sort_keys=True),
                time.time(),
            ),
        )
        self.conn.commit()
        return event_id

    def source_events(self, limit: int = 50) -> list[sqlite3.Row]:
        limit = _validate_limit(limit, "source event limit", 10000)
        return list(
            self.conn.execute(
                "SELECT * FROM source_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        )

    def upsert_finding(
        self,
        finding_id: str,
        task_id: str,
        candidate_sha: str,
        severity: str,
        message: str,
        status: str = "OPEN",
    ) -> str:
        finding_id = _validate_text(finding_id, "finding id")
        task_id = _validate_text(task_id, "task id")
        candidate_sha = _validate_text(candidate_sha, "candidate sha")
        severity = _validate_text(severity, "finding severity")
        message = _validate_text(message, "finding message", 2000)
        status = _validate_text(status, "finding status")
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO findings VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                severity=excluded.severity,
                message=excluded.message,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (finding_id, task_id, candidate_sha, severity, message, status, now, now),
        )
        self.conn.commit()
        return finding_id

    def get_finding(self, finding_id: str) -> sqlite3.Row | None:
        finding_id = _validate_text(finding_id, "finding id")
        return self.conn.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()

    def close_finding(self, finding_id: str) -> None:
        finding_id = _validate_text(finding_id, "finding id")
        self.conn.execute(
            "UPDATE findings SET status=?, updated_at=? WHERE id=?",
            ("RESOLVED", time.time(), finding_id),
        )
        self.conn.commit()

    def open_findings_for_candidate(self, task_id: str, candidate_sha: str) -> list[sqlite3.Row]:
        task_id = _validate_text(task_id, "task id")
        candidate_sha = _validate_text(candidate_sha, "candidate sha")
        return list(
            self.conn.execute(
                "SELECT * FROM findings WHERE task_id=? AND candidate_sha=? AND status='OPEN' ORDER BY created_at",
                (task_id, candidate_sha),
            )
        )

    def add_remediation_attempt(
        self, finding_id: str, status: str, payload: dict[str, Any] | None = None
    ) -> str:
        finding_id = _validate_text(finding_id, "finding id")
        status = _validate_text(status, "remediation status")
        payload = _validate_payload(payload)
        attempt_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO remediation_attempts VALUES (?, ?, ?, ?, ?)",
            (attempt_id, finding_id, status, json.dumps(payload, sort_keys=True), time.time()),
        )
        self.conn.commit()
        return attempt_id

    def remediation_attempt_count(self, finding_id: str) -> int:
        finding_id = _validate_text(finding_id, "finding id")
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM remediation_attempts WHERE finding_id=?",
            (finding_id,),
        ).fetchone()
        return int(row["count"])

    def enqueue_work(
        self,
        task_id: str,
        stage: str,
        worker_id: str | None,
        candidate_sha: str | None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        task_id = _validate_text(task_id, "task id")
        stage = _validate_enum(stage, Stage, "work packet stage")
        worker_id = _validate_optional_text(worker_id, "work packet worker id")
        candidate_sha = _validate_optional_text(candidate_sha, "work packet candidate sha")
        payload = _validate_payload(payload)
        packet_id = str(uuid.uuid4())
        now = time.time()
        self.conn.execute(
            "INSERT INTO work_packets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                packet_id,
                task_id,
                stage,
                worker_id,
                candidate_sha,
                "QUEUED",
                json.dumps(payload, sort_keys=True),
                now,
                now,
            ),
        )
        self.conn.commit()
        return packet_id

    def claim_work_packets(self, worker_id: str, limit: int = 1, lease_seconds: float = 300) -> list[sqlite3.Row]:
        worker_id = _validate_text(worker_id, "worker id")
        limit = _validate_limit(limit, "work packet claim limit", 100)
        if lease_seconds <= 0:
            raise StoreValidationError("work packet lease seconds must be positive")
        with self.conn:
            now = time.time()
            self.conn.execute(
                """
                UPDATE work_packets
                SET status='QUEUED', worker_id=NULL, updated_at=?
                WHERE status='CLAIMED' AND updated_at < ?
                """,
                (now, now - lease_seconds),
            )
            rows = list(
                self.conn.execute(
                    """
                    SELECT * FROM work_packets
                    WHERE status='QUEUED' AND (worker_id IS NULL OR worker_id=?)
                    ORDER BY created_at
                    LIMIT ?
                    """,
                    (worker_id, limit),
                )
            )
            claimed: list[sqlite3.Row] = []
            for row in rows:
                self.conn.execute(
                    "UPDATE work_packets SET status='CLAIMED', worker_id=?, updated_at=? WHERE id=? AND status='QUEUED'",
                    (worker_id, now, row["id"]),
                )
                claimed_row = self.conn.execute("SELECT * FROM work_packets WHERE id=?", (row["id"],)).fetchone()
                if claimed_row is not None:
                    claimed.append(claimed_row)
            return claimed

    def renew_work_packet(self, packet_id: str, worker_id: str) -> bool:
        packet_id = _validate_text(packet_id, "work packet id")
        worker_id = _validate_text(worker_id, "worker id")
        cursor = self.conn.execute(
            """
            UPDATE work_packets
            SET updated_at=?
            WHERE id=? AND worker_id=? AND status='CLAIMED'
            """,
            (time.time(), packet_id, worker_id),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def ack_work_packet(self, packet_id: str, status: str, payload: dict[str, Any] | None = None) -> bool:
        packet_id = _validate_text(packet_id, "work packet id")
        status = _validate_text(status, "work packet status")
        payload = _validate_payload(payload)
        cursor = self.conn.execute(
            "UPDATE work_packets SET status=?, payload=?, updated_at=? WHERE id=? AND status='CLAIMED'",
            (status, json.dumps(payload, sort_keys=True), time.time(), packet_id),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def work_packets(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM work_packets ORDER BY created_at, id"))

    def add_audit_event(self, event_type: str, payload: dict[str, Any] | None = None) -> str:
        event_type = _validate_text(event_type, "audit event type")
        payload = _validate_payload(payload)
        event_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?)",
            (event_id, event_type, json.dumps(payload, sort_keys=True), time.time()),
        )
        self.conn.commit()
        return event_id

    def audit_events(self, limit: int = 500) -> list[sqlite3.Row]:
        limit = _validate_limit(limit, "audit event limit", 10000)
        return list(
            self.conn.execute(
                "SELECT * FROM audit_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        )

    def get_retry_state(self, key: str) -> sqlite3.Row | None:
        key = _validate_text(key, "retry key")
        return self.conn.execute("SELECT * FROM retry_state WHERE key=?", (key,)).fetchone()

    def upsert_retry_state(self, key: str, attempts: int, next_attempt_at: float, reason: str) -> None:
        key = _validate_text(key, "retry key")
        if not isinstance(attempts, int) or attempts < 0:
            raise StoreValidationError("retry attempts must be a non-negative integer")
        reason = _validate_text(reason, "retry reason")
        self.conn.execute(
            """
            INSERT INTO retry_state VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                attempts=excluded.attempts,
                next_attempt_at=excluded.next_attempt_at,
                reason=excluded.reason,
                updated_at=excluded.updated_at
            """,
            (key, attempts, next_attempt_at, reason, time.time()),
        )
        self.conn.commit()

    def clear_retry_state(self, key: str) -> None:
        key = _validate_text(key, "retry key")
        self.conn.execute("DELETE FROM retry_state WHERE key=?", (key,))
        self.conn.commit()

    def retry_states(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM retry_state ORDER BY key"))

    def add_external_evidence(
        self,
        kind: str,
        status: str,
        url: str,
        candidate_sha: str | None = None,
        notes: str = "",
    ) -> str:
        kind = _validate_text(kind, "external evidence kind")
        status = _validate_text(status, "external evidence status")
        url = _validate_text(url, "external evidence url", 1000)
        candidate_sha = _validate_optional_text(candidate_sha, "candidate sha")
        notes = _validate_text(notes, "external evidence notes", 2000) if notes else ""
        if candidate_sha is None:
            existing = self.conn.execute(
                """
                SELECT id FROM external_evidence
                WHERE kind=? AND status=? AND url=? AND candidate_sha IS NULL AND notes=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (kind, status, url, notes),
            ).fetchone()
        else:
            existing = self.conn.execute(
                """
                SELECT id FROM external_evidence
                WHERE kind=? AND status=? AND url=? AND candidate_sha=? AND notes=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (kind, status, url, candidate_sha, notes),
            ).fetchone()
        if existing:
            return str(existing["id"])
        evidence_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO external_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
            (evidence_id, kind, status, url, candidate_sha, notes, time.time()),
        )
        self.conn.commit()
        return evidence_id

    def external_evidence(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM external_evidence ORDER BY created_at DESC"))


def _validate_text(value: str, field: str, max_length: int = 200) -> str:
    if not isinstance(value, str):
        raise StoreValidationError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise StoreValidationError(f"{field} must be a non-empty string")
    if len(normalized) > max_length:
        raise StoreValidationError(f"{field} must be {max_length} characters or fewer")
    return normalized


def _validate_optional_text(value: str | None, field: str, max_length: int = 200) -> str | None:
    if value is None:
        return None
    return _validate_text(value, field, max_length)


def _validate_enum(value, enum_type, field: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise StoreValidationError(f"{field} is unsupported: {value}") from exc


def _validate_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise StoreValidationError("persistence payload must be an object")
    return payload


def _validate_limit(limit: int, field: str, maximum: int) -> int:
    if not isinstance(limit, int):
        raise StoreValidationError(f"{field} must be an integer")
    if limit < 1:
        raise StoreValidationError(f"{field} must be at least 1")
    if limit > maximum:
        raise StoreValidationError(f"{field} must be {maximum} or fewer")
    return limit
