"""Explicit SQLAlchemy database lifecycle for the Build Coordinator.

Importing this module must not create or bind an engine. Callers construct
a `DatabaseLifecycle` (or `configure_process_database`) with an explicit
URL. Separate lifecycles in one process stay isolated.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from build_coordinator.config import BuildCoordinatorSettings, get_settings

CURRENT_SCHEMA_VERSION = 1
SCHEMA_VERSION_TABLE = "build_coordinator_schema_version"


class Base(DeclarativeBase):
    pass


class DatabaseUnconfiguredError(RuntimeError):
    """Raised when process-default database access is used before configure."""


class DatabaseSchemaError(RuntimeError):
    """Raised when coordinator persistence is missing or incompatible."""


class DatabaseLifecycle:
    """Engine/session factory bound to one explicit database configuration."""

    def __init__(
        self,
        database_url: str,
        *,
        data_dir: Path | str | None = None,
    ) -> None:
        if not database_url or not str(database_url).strip():
            raise ValueError("database_url must be non-empty")
        self.database_url = str(database_url).strip()
        self.data_dir = Path(data_dir) if data_dir else None
        self.engine = engine_from_url(self.database_url, data_dir=self.data_dir)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=True,
            autocommit=False,
            future=True,
        )

    @classmethod
    def from_settings(cls, settings: BuildCoordinatorSettings | None = None) -> "DatabaseLifecycle":
        resolved = settings or get_settings()
        return cls(resolved.database_url, data_dir=resolved.data_dir)

    def session(self) -> Session:
        return self.session_factory()

    def initialize_schema(self) -> None:
        from build_coordinator import models  # noqa: F401

        self._ensure_schema_compatible_or_empty()
        Base.metadata.create_all(bind=self.engine)
        self._record_schema_version()

    def _ensure_schema_compatible_or_empty(self) -> None:
        inspector = inspect(self.engine)
        tables = set(inspector.get_table_names())
        if not tables:
            return
        if SCHEMA_VERSION_TABLE not in tables:
            coordinator_tables = {
                table.name for table in Base.metadata.sorted_tables if table.name in tables
            }
            if coordinator_tables:
                raise DatabaseSchemaError(
                    "Build Coordinator database has coordinator tables but no "
                    f"{SCHEMA_VERSION_TABLE} metadata. It may be from an older "
                    "incompatible coordinator version. Refusing to operate without "
                    "an explicit migration or intentional disposable DB recreation."
                )
            return
        with self.engine.connect() as connection:
            version = connection.execute(
                text(
                    f"SELECT version FROM {SCHEMA_VERSION_TABLE} "
                    "WHERE singleton_id = 1"
                )
            ).scalar_one_or_none()
        if version != CURRENT_SCHEMA_VERSION:
            raise DatabaseSchemaError(
                "Build Coordinator database schema version "
                f"{version!r} is not supported by this code "
                f"(expected {CURRENT_SCHEMA_VERSION})."
            )

    def _record_schema_version(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} ("
                    "singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1), "
                    "version INTEGER NOT NULL, "
                    "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL)"
                )
            )
            connection.execute(
                text(
                    f"INSERT INTO {SCHEMA_VERSION_TABLE} "
                    "(singleton_id, version) VALUES (1, :version) "
                    "ON CONFLICT(singleton_id) DO UPDATE SET "
                    "version = excluded.version, updated_at = CURRENT_TIMESTAMP"
                ),
                {"version": CURRENT_SCHEMA_VERSION},
            )

    def dispose(self) -> None:
        self.engine.dispose()


def engine_from_url(database_url: str, *, data_dir: Path | None = None) -> Engine:
    if database_url.startswith("sqlite"):
        if data_dir is not None:
            Path(data_dir).mkdir(parents=True, exist_ok=True)
        elif database_url.startswith("sqlite:///") and not database_url.startswith("sqlite:///:memory:"):
            sqlite_path = Path(database_url.replace("sqlite:///", "", 1))
            if sqlite_path.parent and str(sqlite_path.parent) not in {".", ""}:
                sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            future=True,
        )
    return create_engine(database_url, future=True)


_process_database: DatabaseLifecycle | None = None


def get_process_database() -> DatabaseLifecycle | None:
    return _process_database


def configure_process_database(
    settings: BuildCoordinatorSettings | None = None,
    *,
    database_url: str | None = None,
    data_dir: Path | str | None = None,
) -> DatabaseLifecycle:
    """Bind the process-default lifecycle. CLI and tests call this explicitly."""
    global _process_database
    if database_url is not None:
        lifecycle = DatabaseLifecycle(database_url, data_dir=data_dir)
    else:
        lifecycle = DatabaseLifecycle.from_settings(settings)
    _process_database = lifecycle
    return lifecycle


def reset_process_database() -> None:
    global _process_database
    if _process_database is not None:
        _process_database.dispose()
    _process_database = None


def _require_process_database() -> DatabaseLifecycle:
    global _process_database
    if _process_database is None:
        url = os.getenv("BUILD_COORDINATOR_DATABASE_URL")
        if url:
            data_dir = os.getenv("BUILD_COORDINATOR_DATA_DIR")
            _process_database = DatabaseLifecycle(url, data_dir=data_dir)
        else:
            raise DatabaseUnconfiguredError(
                "Build Coordinator database is not configured. Call "
                "configure_process_database() or construct DatabaseLifecycle explicitly."
            )
    return _process_database


class _SessionLocalProxy:
    """Backward-compatible sessionmaker-like proxy over the process default."""

    def __call__(self, **kwargs):
        return _require_process_database().session_factory(**kwargs)

    def __getattr__(self, name):
        return getattr(_require_process_database().session_factory, name)


class _EngineProxy:
    def __getattr__(self, name):
        return getattr(_require_process_database().engine, name)


SessionLocal = _SessionLocalProxy()
engine = _EngineProxy()


def initialize_schema() -> None:
    _require_process_database().initialize_schema()
