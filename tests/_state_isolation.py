from __future__ import annotations

import os
import tempfile
from pathlib import Path


TEST_GUARD_ENV = "STAGEMESH_TEST_STATE_GUARD"

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="build-coordinator-pytest-"))
_TEST_DB_PATH = _TEST_DATA_DIR / "test-coordinator.sqlite3"


def configure_isolated_test_state() -> None:
    """Force pytest runs onto disposable coordinator state."""
    os.environ[TEST_GUARD_ENV] = "1"
    os.environ["BUILD_COORDINATOR_DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH.as_posix()}"
    os.environ["BUILD_COORDINATOR_DATA_DIR"] = str(_TEST_DATA_DIR)
    os.environ.pop("BUILD_COORDINATOR_CONFIG", None)

    from build_coordinator.db import configure_process_database

    configure_process_database(
        database_url=os.environ["BUILD_COORDINATOR_DATABASE_URL"],
        data_dir=_TEST_DATA_DIR,
    )

