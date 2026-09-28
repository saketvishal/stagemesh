from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class PostgresUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PostgresConnectionInfo:
    dsn: str
    driver: str = "psycopg"


class PostgresStore:
    """Minimal optional PostgreSQL store facade.

    The SQLite Store remains the default runtime. This class gives deployments a
    real psycopg-backed connection path without adding psycopg to the base
    install, keeping local installs no-network and dependency-light.
    """

    name = "postgres"

    def __init__(self, dsn: str):
        self.info = PostgresConnectionInfo(dsn)
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:
            raise PostgresUnavailable("psycopg is required for PostgreSQL storage") from exc
        self._psycopg: Any = psycopg
        self.conn = psycopg.connect(dsn)

    def close(self) -> None:
        self.conn.close()

    def ping(self) -> bool:
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
        return bool(row and row[0] == 1)


def postgres_available() -> bool:
    try:
        __import__("psycopg")
    except ImportError:
        return False
    return True
