"""Test-suite-wide database isolation.

The process-default database is unbound until `configure_process_database`
(or first use of `BUILD_COORDINATOR_DATABASE_URL`). This module still sets
an isolated temp sqlite URL at import time and configures the process
default so tests never consult an operator's `~/.caventra` coordinator
database.

`CAVENTRA_BUILD_CONFIG` and `CAVENTRA_REPO_ROOT` are also cleared so a
stray env var left over from an operator's shell can't reintroduce a
dependency on real coordinator config during the test run.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="build-coordinator-pytest-"))
_TEST_DB_PATH = _TEST_DATA_DIR / "test-coordinator.sqlite3"

os.environ["BUILD_COORDINATOR_DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH.as_posix()}"
os.environ["BUILD_COORDINATOR_DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ.pop("CAVENTRA_BUILD_CONFIG", None)
os.environ.pop("BUILD_COORDINATOR_CONFIG", None)
os.environ.pop("CAVENTRA_REPO_ROOT", None)

from build_coordinator.db import configure_process_database

configure_process_database(
    database_url=os.environ["BUILD_COORDINATOR_DATABASE_URL"],
    data_dir=_TEST_DATA_DIR,
)
