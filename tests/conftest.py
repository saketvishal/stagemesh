from __future__ import annotations

import sys
from pathlib import Path
_ROOT = str(Path(__file__).parent.parent.resolve())
if sys.path[0] != _ROOT:
    sys.path.insert(0, _ROOT)

"""Test-suite-wide database isolation.

The process-default database is unbound until `configure_process_database`
(or first use of `BUILD_COORDINATOR_DATABASE_URL`). This module still sets
an isolated temp sqlite URL at import time and configures the process
default so tests never consult an operator's `~/.build-coordinator` coordinator
database.

`BUILD_COORDINATOR_CONFIG` is also cleared so a stray env var left over from
an operator's shell can't reintroduce a dependency on real coordinator config
during the test run.
"""
from tests._state_isolation import configure_isolated_test_state

configure_isolated_test_state()


import pytest  # noqa: E402


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "registry" / "projects.json"
    monkeypatch.setenv("STAGEMESH_PROJECT_REGISTRY", str(path))
    return path
