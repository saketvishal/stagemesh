from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .persistence import Store


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

    def open(self) -> Store:
        raise NotImplementedError(
            "PostgreSQL support is interface-ready but requires a psycopg-backed Store implementation"
        )
