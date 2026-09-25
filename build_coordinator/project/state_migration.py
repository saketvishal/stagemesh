"""Explicit, backup-first migration for durable state written by an older coordinator.

`DatabaseLifecycle` deliberately refuses a database that has coordinator tables
but no schema-version record. That protects history from accidental
recreation, but it also strands legitimate durable history (for example a
project that ran an earlier vendored coordinator). This module is the
"explicit migration" that refusal asks for: additive for columns, table
rebuilds for changed constraints, rows copied verbatim, a consistent SQLite
backup taken first, and a refusal to run while executions are live.

SQLite only. Other databases must migrate with their own tooling.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateIndex, CreateTable, Table

from build_coordinator import models  # noqa: F401  (registers every table on Base.metadata)
from build_coordinator.db import CURRENT_SCHEMA_VERSION, SCHEMA_VERSION_TABLE, Base
from build_coordinator.project.definition import ProjectError

_LIVE = ("LAUNCHED", "RUNNING")


@dataclass
class MigrationReport:
    database: str
    needed: bool = False
    applied: bool = False
    backup: str | None = None
    tables: list[dict[str, Any]] = field(default_factory=list)
    refused: str | None = None
    stale_execution_reconciliation: dict[str, Any] | None = None
    preservation: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "migration_needed": self.needed,
            "applied": self.applied,
            "backup": self.backup,
            "refused": self.refused,
            "stale_execution_reconciliation": self.stale_execution_reconciliation,
            "tables": self.tables,
            "preservation": self.preservation,
        }


@dataclass
class StaleExecutionReport:
    """Pre-migration reconciliation of execution rows marked LAUNCHED/RUNNING.

    Runs directly against the old-schema SQLite file (raw `sqlite3`, no ORM),
    so it can operate before `migrate_state` opens the database under the
    current schema. Evidence includes the durable claim lease that the normal
    post-migration recovery path (`service.reconcile_stale_executions`) uses
    and the execution's durable recorded process id. An execution is genuinely
    live if its claim is still ACTIVE with an unexpired lease or its recorded
    process is still alive. Everything else is a stale/orphaned record left
    behind by a coordinator process that died without releasing it.
    """

    database: str
    live_examined: int = 0
    genuinely_live: list[dict[str, Any]] = field(default_factory=list)
    reconciled: list[dict[str, Any]] = field(default_factory=list)
    applied: bool = False
    backup: str | None = None
    refused: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "live_examined": self.live_examined,
            "genuinely_live": self.genuinely_live,
            "reconciled": self.reconciled,
            "applied": self.applied,
            "backup": self.backup,
            "refused": self.refused,
        }


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _process_is_alive(process_id: Any) -> bool:
    if process_id in (None, ""):
        return False
    try:
        pid = int(process_id)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    if sys.platform == "win32":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as stat:
            return stat.read().split()[2] != "Z"
    except OSError:
        return True


def _windows_process_is_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_ACCESS_DENIED means the process exists but is not queryable.
        if ctypes.get_last_error() == 5:
            return True
        return False
    try:
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            if ctypes.get_last_error() == 5:
                return True
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def plan_stale_execution_reconciliation(path: Path) -> StaleExecutionReport:
    """Classify every LAUNCHED/RUNNING execution row as genuinely live or stale.

    Read-only: never writes. Refuses to classify (and leaves every row alone)
    if the schema predates claim/lease evidence, since there would be nothing
    durable to distinguish a live process from an orphaned row.
    """
    report = StaleExecutionReport(str(path))
    if not path.is_file():
        return report
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "build_runner_executions" not in tables:
            return report
        exec_columns = {row[1] for row in connection.execute("PRAGMA table_info(build_runner_executions)")}
        if not {"status", "execution_id"} <= exec_columns:
            return report
        live_rows = connection.execute(
            "SELECT * FROM build_runner_executions WHERE status IN ('LAUNCHED', 'RUNNING')"
        ).fetchall()
        report.live_examined = len(live_rows)
        if not live_rows:
            return report
        claim_columns = (
            {row[1] for row in connection.execute("PRAGMA table_info(build_task_claims)")}
            if "build_task_claims" in tables
            else set()
        )
        can_evaluate = (
            "build_task_claims" in tables
            and {"claim_id", "status", "lease_expires_at"} <= claim_columns
            and "claim_id" in exec_columns
        )
        if not can_evaluate:
            report.refused = (
                "cannot distinguish live from stale executions: this schema predates durable "
                "claim/lease evidence (build_task_claims.status/lease_expires_at); reconcile "
                "these rows manually before migrating"
            )
            return report
        now = datetime.now(UTC)
        for row in live_rows:
            claim_id = row["claim_id"]
            claim = (
                connection.execute(
                    "SELECT status, lease_expires_at FROM build_task_claims WHERE claim_id = ?",
                    (claim_id,),
                ).fetchone()
                if claim_id
                else None
            )
            lease_expires_at = _as_utc(claim["lease_expires_at"]) if claim else None
            genuinely_live = (
                claim is not None
                and claim["status"] == "ACTIVE"
                and lease_expires_at is not None
                and lease_expires_at > now
            )
            process_id = row["process_id"] if "process_id" in exec_columns else None
            process_alive = _process_is_alive(process_id)
            entry = {
                "execution_id": row["execution_id"],
                "task_id": row["task_id"],
                "status": row["status"],
                "claim_id": claim_id,
                "claim_status": claim["status"] if claim else None,
                "lease_expires_at": claim["lease_expires_at"] if claim else None,
                "process_id": process_id,
                "process_alive": process_alive,
            }
            (report.genuinely_live if genuinely_live or process_alive else report.reconciled).append(entry)
        if report.genuinely_live:
            report.refused = (
                f"{len(report.genuinely_live)} execution(s) have live process evidence or an active, "
                "unexpired claim lease; wait for them to finish before migrating"
            )
        return report
    finally:
        connection.close()


def reconcile_stale_executions(path: Path, *, apply: bool) -> StaleExecutionReport:
    """Backup-first transition of exactly the stale execution rows to LOST.

    Only `build_runner_executions.status` is touched, and only for the rows
    `plan_stale_execution_reconciliation` classified as stale. Task history,
    checkpoints and worktree metadata are left untouched so the normal
    post-migration recovery path (`service.recover_lost_execution_claims`)
    can resume the task from a replacement worker, exactly as it would for an
    execution lost after a normal restart.
    """
    report = plan_stale_execution_reconciliation(path)
    if not apply or report.refused or not report.reconciled:
        return report

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.pre-reconcile-{stamp}.bak")
    source = sqlite3.connect(str(path))
    try:
        target = sqlite3.connect(str(backup))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    report.backup = str(backup)

    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        try:
            now = datetime.now(UTC).isoformat()
            for entry in report.reconciled:
                row = connection.execute(
                    "SELECT result_data FROM build_runner_executions WHERE execution_id = ?",
                    (entry["execution_id"],),
                ).fetchone()
                if row is None:
                    continue
                try:
                    result_data = json.loads(row["result_data"]) if row["result_data"] else {}
                except (TypeError, json.JSONDecodeError):
                    result_data = {}
                result_data["reconciliation_state"] = "STALE_EXECUTION_PRE_MIGRATION"
                connection.execute(
                    "UPDATE build_runner_executions SET status = 'LOST', completed_at = ?, "
                    "last_observed_at = ?, result_data = ? "
                    "WHERE execution_id = ? AND status IN ('LAUNCHED', 'RUNNING')",
                    (now, now, json.dumps(result_data), entry["execution_id"]),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    report.applied = True
    return report


def sqlite_path_from_url(database_url: str) -> Path:
    if not database_url.startswith("sqlite:///"):
        raise ProjectError("state migration supports SQLite databases only")
    return Path(database_url.replace("sqlite:///", "", 1))


def _normal(sql: str | None) -> str:
    return re.sub(r"\s+", " ", (sql or "")).strip().rstrip(";")


def _model_ddl(table: Table, *, name: str | None = None) -> str:
    ddl = str(CreateTable(table).compile(dialect=sqlite_dialect.dialect()))
    if name:
        ddl = re.sub(r"^\s*CREATE TABLE \S+", f"CREATE TABLE {name}", ddl, count=1)
    return ddl


def _same_ddl(actual: str | None, table: Table) -> bool:
    return _normal(actual).replace('"', "") == _normal(_model_ddl(table)).replace('"', "")


def _missing_indexes(connection: sqlite3.Connection, table: Table) -> list[str]:
    present = {row[1] for row in connection.execute(f"PRAGMA index_list({table.name})")}
    return [index.name for index in table.indexes if index.name not in present]


def _schema_differences(connection: sqlite3.Connection) -> list[str]:
    present = {
        row[0]: row[1]
        for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
    }
    return [
        table.name
        for table in Base.metadata.sorted_tables
        if table.name in present and (not _same_ddl(present[table.name], table) or _missing_indexes(connection, table))
    ]


def schema_repair_items(engine: Engine) -> list[dict[str, Any]]:
    """Physical schema compatibility checks for already-versioned databases."""
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    items: list[dict[str, Any]] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in present:
            items.append({"table": table.name, "action": "CREATE"})
    if items:
        return items
    if engine.dialect.name == "sqlite":
        dbapi = engine.raw_connection()
        try:
            for table in _schema_differences(dbapi):
                items.append({"table": table, "action": "REBUILD"})
        finally:
            dbapi.close()
    return items


def _default_for(column) -> Any:
    default = getattr(column, "default", None)
    if default is None:
        return None
    arg = default.arg
    if callable(arg):
        try:
            arg = arg(None)
        except TypeError:
            arg = arg()
    if isinstance(arg, (list, dict)):
        import json

        return json.dumps(arg)
    if isinstance(arg, bool):
        return int(arg)
    return arg


def inventory(path: Path) -> dict[str, dict[str, Any]]:
    """Row count and content digest per table, over every column the table has."""
    connection = sqlite3.connect(str(path))
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {name: _table_digest(connection, name, None) for name in names}
    finally:
        connection.close()


def _table_digest(connection: sqlite3.Connection, table: str, columns: list[str] | None) -> dict[str, Any]:
    have = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    use = [c for c in (columns if columns is not None else have) if c in have]
    if not use:
        return {"rows": 0, "digest": None, "columns": []}
    order = ", ".join(use)
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(f"SELECT {', '.join(use)} FROM {table} ORDER BY {order}"):
        digest.update(json.dumps(row, default=str, sort_keys=True).encode("utf-8"))
        count += 1
    return {"rows": count, "digest": digest.hexdigest(), "columns": use}


def verify_preservation(before: Path, after: Path) -> list[dict[str, Any]]:
    """Every table and every pre-existing column must survive with identical content."""
    old = sqlite3.connect(str(before))
    new = sqlite3.connect(str(after))
    try:
        results: list[dict[str, Any]] = []
        tables = [
            row[0]
            for row in old.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            # the schema version row is expected to change (bump) as part of
            # migrating a database that already had one; it is metadata about
            # the migration, not durable task history to preserve verbatim.
            if row[0] != SCHEMA_VERSION_TABLE
        ]
        for table in tables:
            have_old = [row[1] for row in old.execute(f"PRAGMA table_info({table})")]
            have_new = [row[1] for row in new.execute(f"PRAGMA table_info({table})")]
            surviving = [c for c in have_old if c in have_new]
            before_inv = _table_digest(old, table, surviving)
            after_inv = _table_digest(new, table, surviving)
            results.append(
                {
                    "table": table,
                    "rows_before": before_inv["rows"],
                    "rows_after": after_inv["rows"],
                    "columns_compared": surviving,
                    "identical": before_inv == after_inv,
                }
            )
        return results
    finally:
        old.close()
        new.close()


def plan_migration(path: Path) -> MigrationReport:
    report = MigrationReport(str(path))
    if not Base.metadata.sorted_tables:
        raise ProjectError("coordinator models are not registered; refusing to plan a migration")
    if not path.is_file():
        return report
    connection = sqlite3.connect(str(path))
    try:
        present = {
            row[0]: row[1]
            for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
        }
        stored_version = None
        if SCHEMA_VERSION_TABLE in present:
            stored_version = connection.execute(
                f"SELECT version FROM {SCHEMA_VERSION_TABLE} WHERE singleton_id = 1"
            ).fetchone()
            stored_version = stored_version[0] if stored_version else None
        for table in Base.metadata.sorted_tables:
            if table.name not in present:
                report.tables.append(
                    {
                        "table": table.name,
                        "action": "CREATE",
                        "columns_added": [column.name for column in table.columns],
                        "columns_dropped": [],
                        "rows": 0,
                    }
                )
                continue
            missing_indexes = _missing_indexes(connection, table)
            if _same_ddl(present[table.name], table) and not missing_indexes:
                continue
            have = [row[1] for row in connection.execute(f"PRAGMA table_info({table.name})")]
            want = [column.name for column in table.columns]
            report.tables.append(
                {
                    "table": table.name,
                    "action": "REBUILD",
                    "columns_added": [c for c in want if c not in have],
                    "columns_dropped": [c for c in have if c not in want],
                    "indexes_added": missing_indexes,
                    "rows": connection.execute(f"SELECT COUNT(*) FROM {table.name}").fetchone()[0],
                }
            )
        report.needed = bool(report.tables) or stored_version != CURRENT_SCHEMA_VERSION
        live = 0
        if "build_runner_executions" in present:
            live = connection.execute(
                "SELECT COUNT(*) FROM build_runner_executions WHERE status IN (?, ?)", _LIVE
            ).fetchone()[0]
        if live:
            stale_report = plan_stale_execution_reconciliation(path)
            report.stale_execution_reconciliation = stale_report.as_dict()
            if stale_report.refused:
                report.refused = (
                    f"{live} live execution(s); {stale_report.refused}"
                )
            elif stale_report.reconciled:
                report.refused = (
                    f"{live} live execution(s); {len(stale_report.reconciled)} appear stale/orphaned. "
                    "Run `stagemesh project migrate-state --apply` to take a backup, mark exactly "
                    "those stale execution row(s) LOST, and retry migration."
                )
            else:
                report.refused = f"{live} live execution(s); wait for them to finish before migrating"
        return report
    finally:
        connection.close()


def migrate_state(path: Path, *, apply: bool) -> MigrationReport:
    report = plan_migration(path)
    if not apply or not report.needed or report.refused:
        return report

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.pre-stagemesh-{stamp}.bak")
    source = sqlite3.connect(str(path))
    try:
        target = sqlite3.connect(str(backup))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    report.backup = str(backup)

    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN")
        try:
            rebuilt = {item["table"] for item in report.tables}
            for table in Base.metadata.sorted_tables:
                if table.name not in rebuilt:
                    continue
                action = next(item["action"] for item in report.tables if item["table"] == table.name)
                if action == "CREATE":
                    _create_table(connection, table)
                else:
                    _rebuild(connection, table)
            leftover = _schema_differences(connection)
            if leftover:
                raise ProjectError(f"schema still differs from the current models after rebuild: {leftover}")
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} ("
                "singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1), "
                "version INTEGER NOT NULL, "
                "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL)"
            )
            connection.execute(
                f"INSERT OR REPLACE INTO {SCHEMA_VERSION_TABLE} (singleton_id, version) VALUES (1, ?)",
                (CURRENT_SCHEMA_VERSION,),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        problems = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    report.preservation = verify_preservation(backup, path)
    failed = [r for r in report.preservation if not r["identical"]]
    if problems or integrity != "ok" or failed:
        shutil.copyfile(backup, path)
        raise ProjectError(
            f"migration verification failed (fk_problems={len(problems)}, integrity={integrity}, "
            f"changed_tables={[r['table'] for r in failed]}); the database was restored from {backup}"
        )
    report.applied = True
    return report


def _rebuild(connection: sqlite3.Connection, table: Table) -> None:
    staging = f"{table.name}__stagemesh_new"
    connection.execute(_model_ddl(table, name=staging))
    have = {row[1] for row in connection.execute(f"PRAGMA table_info({table.name})")}
    columns: list[str] = []
    selects: list[str] = []
    params: list[Any] = []
    for column in table.columns:
        columns.append(column.name)
        if column.name in have:
            selects.append(column.name)
            continue
        selects.append("?")
        params.append(_default_for(column))
    connection.execute(
        f"INSERT INTO {staging} ({', '.join(columns)}) SELECT {', '.join(selects)} FROM {table.name}",
        params,
    )
    connection.execute(f"DROP TABLE {table.name}")
    connection.execute(f"ALTER TABLE {staging} RENAME TO {table.name}")
    for index in table.indexes:
        connection.execute(str(CreateIndex(index).compile(dialect=sqlite_dialect.dialect())))


def _create_table(connection: sqlite3.Connection, table: Table) -> None:
    connection.execute(_model_ddl(table))
    for index in table.indexes:
        connection.execute(str(CreateIndex(index).compile(dialect=sqlite_dialect.dialect())))
