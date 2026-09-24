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
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.dialects import sqlite as sqlite_dialect
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
    preservation: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "migration_needed": self.needed,
            "applied": self.applied,
            "backup": self.backup,
            "refused": self.refused,
            "tables": self.tables,
            "preservation": self.preservation,
        }


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


def _schema_differences(connection: sqlite3.Connection) -> list[str]:
    present = {
        row[0]: row[1]
        for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
    }
    return [
        table.name
        for table in Base.metadata.sorted_tables
        if table.name in present and not _same_ddl(present[table.name], table)
    ]


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
        if SCHEMA_VERSION_TABLE in present:
            stored_version = connection.execute(
                f"SELECT version FROM {SCHEMA_VERSION_TABLE} WHERE singleton_id = 1"
            ).fetchone()
            stored_version = stored_version[0] if stored_version else None
            if stored_version == CURRENT_SCHEMA_VERSION:
                return report
        for table in Base.metadata.sorted_tables:
            if table.name not in present:
                continue
            if _same_ddl(present[table.name], table):
                continue
            have = [row[1] for row in connection.execute(f"PRAGMA table_info({table.name})")]
            want = [column.name for column in table.columns]
            report.tables.append(
                {
                    "table": table.name,
                    "action": "REBUILD",
                    "columns_added": [c for c in want if c not in have],
                    "columns_dropped": [c for c in have if c not in want],
                    "rows": connection.execute(f"SELECT COUNT(*) FROM {table.name}").fetchone()[0],
                }
            )
        report.needed = True
        live = 0
        if "build_runner_executions" in present:
            live = connection.execute(
                "SELECT COUNT(*) FROM build_runner_executions WHERE status IN (?, ?)", _LIVE
            ).fetchone()[0]
        if live:
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
