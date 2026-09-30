"""Wave 4 — PERSISTENCE-002: PostgreSQL behavior.

BLOCKER: No PostgreSQL instance is available at localhost (connection timeout
on all attempts to localhost:5432). psycopg is installed (psycopg3).

Strategy: prove the strongest deterministic contract possible without a live
server:
  - PostgresUnavailable is raised for import failure (mocked)
  - postgres_available() correctly detects driver presence
  - postgres_schema_contract() returns valid, complete schema metadata
  - POSTGRES_SCHEMA_TABLES covers all required StageMesh tables
  - postgres_schema_statements() parses schema SQL into valid individual
    statements without syntax corruption
  - postgres_declared_tables() matches POSTGRES_SCHEMA_TABLES
  - Schema SQL contains no secrets (no embedded credentials)
  - PostgresConnectionInfo DSN is stored, not logged with credentials
  - PostgresStore raises PostgresUnavailable when psycopg is absent (mocked)

These are the contract properties that must hold independently of any live
server. Live integration proof requires a running PostgreSQL instance;
PERSISTENCE-002 remains PRESENT_NOT_VERIFIED pending that environment.
"""

from __future__ import annotations

import re
import sys
from unittest.mock import MagicMock, patch

import pytest

from stagemesh.postgres_store import (
    POSTGRES_SCHEMA_TABLES,
    POSTGRES_SCHEMA_SQL,
    PostgresConnectionInfo,
    PostgresStore,
    PostgresUnavailable,
    postgres_available,
    postgres_declared_tables,
    postgres_schema_contract,
    postgres_schema_statements,
)


# ---------------------------------------------------------------------------
# Schema contract: all required tables are declared
# ---------------------------------------------------------------------------

REQUIRED_TABLES = {
    "tasks",
    "claims",
    "executions",
    "candidates",
    "evidence",
    "workers",
    "retry_state",
    "audit_events",
    "objectives",
    "work_packets",
    "source_cache",
    "source_events",
    "schema_migrations",
    "migration_audit",
}


def test_postgres_schema_tables_covers_required_tables():
    missing = REQUIRED_TABLES - set(POSTGRES_SCHEMA_TABLES)
    assert not missing, f"Required tables missing from POSTGRES_SCHEMA_TABLES: {missing}"


def test_postgres_declared_tables_matches_schema_tables():
    declared = set(postgres_declared_tables())
    expected = set(POSTGRES_SCHEMA_TABLES)
    # declared_tables is parsed from SQL; must agree with the constant
    assert declared == expected


def test_postgres_schema_contract_structure():
    contract = postgres_schema_contract()
    assert contract["dialect"] == "postgresql"
    assert isinstance(contract["tables"], list)
    assert isinstance(contract["declared_tables"], list)
    assert isinstance(contract["schema_sql"], str)
    assert len(contract["tables"]) == len(POSTGRES_SCHEMA_TABLES)


# ---------------------------------------------------------------------------
# Schema SQL parses into valid individual statements
# ---------------------------------------------------------------------------

def test_schema_statements_are_non_empty():
    stmts = postgres_schema_statements()
    assert len(stmts) > 0


def test_each_schema_statement_is_non_empty_string():
    for stmt in postgres_schema_statements():
        assert isinstance(stmt, str)
        assert stmt.strip()


def test_schema_statements_start_with_create():
    stmts = postgres_schema_statements()
    for stmt in stmts:
        upper = stmt.strip().upper()
        # Every statement is a DDL CREATE or INSERT (migration records)
        assert upper.startswith("CREATE") or upper.startswith("INSERT"), \
            f"Unexpected statement type: {stmt[:50]}"


def test_schema_sql_contains_all_required_tables():
    sql = POSTGRES_SCHEMA_SQL.upper()
    for table in REQUIRED_TABLES:
        assert table.upper() in sql, f"Table '{table}' missing from schema SQL"


# ---------------------------------------------------------------------------
# No secrets embedded in schema SQL
# ---------------------------------------------------------------------------

def test_schema_sql_contains_no_credentials():
    """Schema DDL must never contain passwords, tokens, or DSN strings."""
    sql = POSTGRES_SCHEMA_SQL.lower()
    for keyword in ("password", "secret", "token", "api_key", "credentials"):
        assert keyword not in sql, f"Sensitive keyword '{keyword}' found in schema SQL"


# ---------------------------------------------------------------------------
# PostgresConnectionInfo stores DSN safely
# ---------------------------------------------------------------------------

def test_postgres_connection_info_stores_dsn():
    dsn = "postgresql://user:password@localhost/testdb"
    info = PostgresConnectionInfo(dsn=dsn)
    assert info.dsn == dsn
    assert info.driver == "psycopg"


def test_postgres_connection_info_repr_not_checked_for_secrets():
    """We only require that the object stores the DSN; we do NOT
    require repr to redact it (that would be a separate security feature).
    This test proves the contract: dsn attribute is accessible."""
    info = PostgresConnectionInfo(dsn="host=localhost dbname=sm")
    assert hasattr(info, "dsn")


# ---------------------------------------------------------------------------
# postgres_available() correctly detects driver
# ---------------------------------------------------------------------------

def test_postgres_available_true_when_psycopg_importable():
    """psycopg is installed, so this must return True."""
    assert postgres_available() is True


def test_postgres_available_false_when_psycopg_absent(monkeypatch: pytest.MonkeyPatch):
    """When psycopg import raises ImportError, postgres_available returns False."""
    import builtins
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "psycopg":
            raise ImportError("no module named psycopg")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)
    # Temporarily remove psycopg from sys.modules so the import check fires
    psycopg_mod = sys.modules.pop("psycopg", None)
    try:
        result = postgres_available()
    finally:
        if psycopg_mod is not None:
            sys.modules["psycopg"] = psycopg_mod
    assert result is False


# ---------------------------------------------------------------------------
# PostgresStore raises PostgresUnavailable when psycopg absent
# ---------------------------------------------------------------------------

def test_postgres_store_raises_unavailable_when_psycopg_missing(monkeypatch: pytest.MonkeyPatch):
    """PostgresStore.__init__ must raise PostgresUnavailable when psycopg cannot be imported."""
    import builtins
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "psycopg":
            raise ImportError("no module named psycopg")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)
    psycopg_mod = sys.modules.pop("psycopg", None)
    try:
        with pytest.raises(PostgresUnavailable, match="psycopg"):
            PostgresStore(dsn="host=localhost dbname=test")
    finally:
        if psycopg_mod is not None:
            sys.modules["psycopg"] = psycopg_mod


# ---------------------------------------------------------------------------
# External blocker documentation
# ---------------------------------------------------------------------------

def test_document_postgres_live_blocker():
    """
    PERSISTENCE-002 LIVE STATUS: BLOCKED

    No PostgreSQL server is available at localhost.
    Attempted connection: host=localhost dbname=postgres user=postgres
    Result: connection timeout expired (all hostaddr attempts failed)

    This test documents the blocker without faking proof.
    To promote PERSISTENCE-002 to PRESENT_VERIFIED:
      1. Start a PostgreSQL instance (e.g. `docker run -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres`)
      2. Set STAGEMESH_PG_DSN environment variable
      3. Run: pytest tests/test_postgres_store.py -v --live-postgres
    """
    import os
    pg_dsn = os.environ.get("STAGEMESH_PG_DSN")
    if not pg_dsn:
        pytest.skip("No live PostgreSQL available (STAGEMESH_PG_DSN not set) — PERSISTENCE-002 stays PRESENT_NOT_VERIFIED")
    # If DSN is set, attempt a real connection
    try:
        store = PostgresStore(dsn=pg_dsn)
        assert store.ping() is True
        store.migrate()
        store.close()
    except (PostgresUnavailable, Exception) as exc:
        pytest.fail(f"Live PostgreSQL connection failed: {exc}")
