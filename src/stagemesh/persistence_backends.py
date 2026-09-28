from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .persistence import Store
from .postgres_store import PostgresStore


class PersistenceBackend(Protocol):
    name: str

    def open(self) -> Store:
        ...


@dataclass(frozen=True)
class SQLiteBackend:
    db_path: Path
    name: str = "sqlite"

    def open(self) -> Store:
        store = Store(self.db_path)
        store.migrate()
        return store


@dataclass(frozen=True)
class PostgresBackend:
    dsn: str
    name: str = "postgres"

    def open(self) -> PostgresStore:
        return PostgresStore(self.dsn)


@dataclass(frozen=True)
class BackendProbe:
    name: str
    available: bool
    reason: str


def probe_backend(url: str | None, sqlite_path: Path | None = None) -> BackendProbe:
    if not url or url.startswith("sqlite://") or sqlite_path is not None:
        return BackendProbe("sqlite", True, "sqlite default available")
    if url.startswith("postgres://") or url.startswith("postgresql://"):
        try:
            __import__("psycopg")
        except ImportError:
            return BackendProbe("postgres", False, "psycopg is not installed")
        return BackendProbe("postgres", True, "psycopg is installed")
    return BackendProbe("unknown", False, f"unsupported backend URL: {url}")
