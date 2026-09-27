"""Explicit SQLAlchemy database lifecycle for the Build Coordinator.

Importing this module must not create or bind an engine. Callers construct
a `DatabaseLifecycle` (or `configure_process_database`) with an explicit
URL. Separate lifecycles in one process stay isolated.
"""

from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import urlparse
from urllib.request import url2pathname

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from build_coordinator.config import BuildCoordinatorSettings, get_settings

CURRENT_SCHEMA_VERSION = 8
SCHEMA_VERSION_TABLE = "build_coordinator_schema_version"

# Bounded wait a single SQLite connection will block for another writer's
# transaction before raising "database is locked". This is deliberately
# short: a valid single coordinator lifecycle should never hold a write
# transaction this long, so a timeout this size only absorbs the momentary
# contention SQLite itself imposes around a single writer, not sustained
# multi-writer contention (that is what the coordinator lock in
# `coordinator_lock.py` exists to prevent).
SQLITE_BUSY_TIMEOUT_MS = 5_000


class Base(DeclarativeBase):
    pass


class DatabaseUnconfiguredError(RuntimeError):
    """Raised when process-default database access is used before configure."""


class DatabaseSchemaError(RuntimeError):
    """Raised when coordinator persistence is missing or incompatible."""


class TestStateIsolationError(RuntimeError):
    """Raised when tests try to bind the real project durable state."""


def _sqlite_path_from_url(database_url: str) -> Path | None:
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        return None
    if parsed.path in {"", "/:memory:"}:
        return None
    if parsed.netloc:
        path = url2pathname(parsed.path)
        return Path(f"//{parsed.netloc}{path}").expanduser().resolve()
    if parsed.path.startswith("/") and not parsed.path.startswith("//"):
        # SQLAlchemy treats sqlite:///foo.db as a relative path, even though
        # urlparse exposes the path as /foo.db. Resolve it the same way the
        # engine will open it so the test-state guard cannot be bypassed.
        return Path(url2pathname(parsed.path[1:])).expanduser().resolve()
    path = url2pathname(parsed.path)
    return Path(path).expanduser().resolve()


def _active_project_state_dirs() -> set[Path]:
    candidates: set[Path] = set()
    roots = [Path.cwd(), Path(__file__).resolve()]
    for env_name in ("BUILD_COORDINATOR_REPO_ROOT", "REPO_ROOT"):
        configured = os.getenv(env_name)
        if configured:
            roots.append(Path(configured).expanduser())
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root.absolute()
        parts = resolved.parts
        for index, part in enumerate(parts):
            if part == ".build-coordinator":
                candidates.add(Path(*parts[: index + 1]).resolve())
        candidates.add((resolved / ".build-coordinator").resolve())
        try:
            candidates.add((resolved.parents[1] / ".build-coordinator").resolve())
        except IndexError:
            pass
    return candidates


def _is_path_within(path: Path | None, directory: Path) -> bool:
    if path is None:
        return False
    return path == directory or path.is_relative_to(directory)


def _assert_test_state_isolated(database_url: str, data_dir: Path | str | None) -> None:
    if os.getenv("STAGEMESH_TEST_STATE_GUARD") != "1":
        return

    db_path = _sqlite_path_from_url(database_url)
    resolved_data_dir = Path(data_dir).expanduser().resolve() if data_dir is not None else None
    active_state_dirs = _active_project_state_dirs()
    for state_dir in active_state_dirs:
        if _is_path_within(resolved_data_dir, state_dir) or _is_path_within(db_path, state_dir):
            raise TestStateIsolationError(
                "Refusing to run tests against the active project durable state: "
                f"database_url={database_url!r}, data_dir={str(data_dir)!r}"
            )


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
        _assert_test_state_isolated(self.database_url, self.data_dir)
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
        coordinator_tables = {
            table.name for table in Base.metadata.sorted_tables if table.name in tables
        }
        if not coordinator_tables:
            return
        if SCHEMA_VERSION_TABLE not in tables:
            raise DatabaseSchemaError(
                "Build Coordinator database has coordinator tables but no "
                f"{SCHEMA_VERSION_TABLE} metadata. It may be from an older "
                "incompatible coordinator version. Refusing to operate without "
                "an explicit migration or intentional disposable DB recreation."
            )
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
                f"(expected {CURRENT_SCHEMA_VERSION}). Run "
                "`stagemesh project migrate-state --all` to review and apply "
                "backup-first migrations for registered projects."
            )
        from build_coordinator.project.state_migration import schema_repair_items

        repairs = schema_repair_items(self.engine)
        if repairs:
            summary = ", ".join(f"{item['action']} {item['table']}" for item in repairs[:5])
            raise DatabaseSchemaError(
                "Build Coordinator database schema is incomplete or incompatible "
                f"despite version {CURRENT_SCHEMA_VERSION}: {summary}. Run "
                "`stagemesh project migrate-state --all` to repair registered "
                "projects with a backup-first migration."
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
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            future=True,
        )
        _configure_sqlite_pragmas(engine, in_memory=":memory:" in database_url)
        return engine
    return create_engine(database_url, future=True)


def _configure_sqlite_pragmas(engine: Engine, *, in_memory: bool) -> None:
    """StageMesh's SQLite access pattern is one coordinator process issuing
    short, discrete write transactions per orchestration cycle, with
    occasional concurrent readers (CLI status/inspection commands) against
    the same file. WAL lets readers proceed without blocking on the
    coordinator's writer, and a short `busy_timeout` absorbs the brief
    window where SQLite itself briefly holds the write lock -- it is not a
    substitute for the single-coordinator lock (`coordinator_lock.py`),
    which is what prevents sustained multi-writer contention in the first
    place.
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            if not in_memory:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        finally:
            cursor.close()


class DatabaseBusyError(RuntimeError):
    """Raised when SQLite write contention persisted through every retry.

    A transient "database is locked"/"database is busy" error inside one
    valid coordinator lifecycle is retried with bounded backoff (see
    `with_sqlite_retry`); this is only raised once that budget is
    exhausted, so callers can treat it as a real, observable failure rather
    than a silent crash.
    """

    def __str__(self) -> str:
        return f"STAGEMESH_SQLITE_BUSY: {self.args[0]}" if self.args else "STAGEMESH_SQLITE_BUSY"


def _is_transient_sqlite_lock_error(exc: OperationalError) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    return "database is locked" in message or "database is busy" in message


_T = TypeVar("_T")


def with_sqlite_retry(
    fn: Callable[[], _T],
    *,
    attempts: int = 5,
    base_delay: float = 0.05,
    max_delay: float = 1.0,
    is_retryable: Callable[[OperationalError], bool] = _is_transient_sqlite_lock_error,
) -> _T:
    """Run `fn`, retrying with bounded exponential backoff if it fails on
    transient SQLite writer contention.

    `fn` must be safe to call more than once from scratch (e.g. open its own
    session/transaction internally) since a failed attempt is abandoned
    entirely, not resumed. Any other exception, or lock contention that
    outlasts `attempts`, propagates (lock contention becomes
    `DatabaseBusyError` so it is distinguishable from a real query/logic
    bug).

    `is_retryable` lets a caller narrow which `OperationalError`s are worth
    retrying beyond the default transient-lock check -- e.g. a caller that
    started a non-idempotent side effect partway through `fn` and must not
    retry once that has happened.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except DatabaseBusyError as exc:
            if attempt == attempts:
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            time.sleep(delay + random.uniform(0, base_delay))
        except OperationalError as exc:
            if not is_retryable(exc):
                raise
            if attempt == attempts:
                raise DatabaseBusyError(
                    f"SQLite write contention persisted after {attempts} attempts: {exc}"
                ) from exc
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            time.sleep(delay + random.uniform(0, base_delay))
    raise AssertionError("unreachable")  # pragma: no cover


def commit_or_busy(session: Session) -> None:
    """Commit `session`, converting SQLite writer-lock contention into a
    typed `DatabaseBusyError` instead of letting a raw `OperationalError`
    escape to the caller.

    This intentionally does not retry the commit itself: once `commit()`
    fails, SQLAlchemy has already rolled the transaction back and expired
    the session's pending object state, so silently calling `commit()`
    again would just commit an empty transaction and look like success
    while dropping the write. The project's SQLite connections already
    apply a bounded `busy_timeout` PRAGMA, which is where the actual
    bounded wait for transient contention happens before this ever raises;
    this only decides what a caller sees once that wait is exhausted.
    """
    try:
        session.commit()
    except OperationalError as exc:
        session.rollback()
        if not _is_transient_sqlite_lock_error(exc):
            raise
        raise DatabaseBusyError(f"SQLite write contention on commit: {exc}") from exc


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
