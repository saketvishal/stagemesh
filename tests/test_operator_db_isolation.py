"""Canary proof that running the coordinator test suite can never connect
to or mutate an operator's real, canonical coordinator database.

`conftest.py` in this directory forces an isolated temp sqlite database via
`BUILD_COORDINATOR_DATABASE_URL` before any coordinator module binds its
engine. This test proves that protection actually holds by simulating a
real operator machine: a fake `$HOME` with a real
`.build-coordinator/config.json` pointing at a "canary" database that
already has data in it, then running the coordinator test suite as a
genuine subprocess against that fake home, and asserting the canary
database is byte-for-byte untouched and never received the coordinator's
schema.
"""

from __future__ import annotations
import pytest

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _isolated_nested_pytest_env(tmp_path: Path) -> tuple[dict, Path]:
    """Give nested pytest its own TEMP/TMP/--basetemp so it never depends on
    the operator's global Windows TEMP (pytest-of-<user>) permissions."""
    isolated_tmp = tmp_path / "nested-pytest-tmp"
    isolated_tmp.mkdir()
    basetemp = tmp_path / "nested-pytest-basetemp"
    env = dict(os.environ)
    env["TEMP"] = str(isolated_tmp)
    env["TMP"] = str(isolated_tmp)
    env["TMPDIR"] = str(isolated_tmp)
    env["PYTHONPYCACHEPREFIX"] = str(isolated_tmp / "pycache")
    env.pop("PYTEST_ADDOPTS", None)
    return env, basetemp


def _create_canary_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE canary_marker (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO canary_marker (note) VALUES ('do-not-touch')")
        conn.commit()
    finally:
        conn.close()


def _canary_snapshot(path: Path) -> tuple[bytes, list[str], list[tuple]]:
    raw = path.read_bytes()
    conn = sqlite3.connect(str(path))
    try:
        tables = sorted(
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
        rows = list(conn.execute("SELECT id, note FROM canary_marker"))
    finally:
        conn.close()
    return raw, tables, rows


@pytest.mark.timeout(900)
def test_running_coordinator_tests_never_touches_operator_canary_db(tmp_path: Path):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    canary_db_path = fake_home / "canary-operator-data" / "canary.sqlite3"
    _create_canary_database(canary_db_path)

    config_dir = fake_home / ".build-coordinator"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "control_repo_root": str(fake_home / "control-repo"),
                "database_url": f"sqlite:///{canary_db_path.as_posix()}",
                "data_dir": str(canary_db_path.parent),
            }
        ),
        encoding="utf-8",
    )

    before_raw, before_tables, before_rows = _canary_snapshot(canary_db_path)
    assert before_tables == ["canary_marker"]
    assert before_rows == [(1, "do-not-touch")]

    env, basetemp = _isolated_nested_pytest_env(tmp_path)
    # Simulate a real operator machine: the default coordinator config path
    # (~/.build-coordinator/config.json) resolves to the canary config
    # above via a redirected home directory. Deliberately do NOT set
    # BUILD_COORDINATOR_DATABASE_URL or BUILD_COORDINATOR_CONFIG here -- the
    # protection under test is conftest.py's own, unconditional override,
    # not anything this test's own env setup provides.
    for var in ("BUILD_COORDINATOR_DATABASE_URL", "BUILD_COORDINATOR_CONFIG", "BUILD_COORDINATOR_REPO_ROOT"):
        env.pop(var, None)
    env["HOME"] = str(fake_home)
    env["USERPROFILE"] = str(fake_home)
    env["PYTHONPATH"] = str(REPO_ROOT)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            # Exclude this file itself: it spawns a subprocess running the
            # coordinator suite, and without excluding itself that would
            # recurse into another full suite run (and another) forever.
            "--ignore=tests/test_operator_db_isolation.py",
            f"--basetemp={basetemp}",
            "-p",
            "no:cacheprovider",
            "-q",
            "--no-header",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )

    assert result.returncode == 0, (
        f"coordinator test suite failed under a redirected HOME:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    after_raw, after_tables, after_rows = _canary_snapshot(canary_db_path)
    assert after_tables == before_tables, "canary DB gained tables -- coordinator schema leaked in"
    assert after_rows == before_rows, "canary DB row contents changed"
    assert after_raw == before_raw, "canary DB file bytes changed -- it was written to"
    assert basetemp.exists(), "nested pytest did not use the isolated --basetemp"


def test_targeted_objective_runner_file_never_touches_operator_canary_db(tmp_path: Path):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    canary_db_path = fake_home / "canary-operator-data" / "canary.sqlite3"
    _create_canary_database(canary_db_path)

    config_dir = fake_home / ".build-coordinator"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "control_repo_root": str(fake_home / "control-repo"),
                "database_url": f"sqlite:///{canary_db_path.as_posix()}",
                "data_dir": str(canary_db_path.parent),
            }
        ),
        encoding="utf-8",
    )

    before_raw, before_tables, before_rows = _canary_snapshot(canary_db_path)

    env, basetemp = _isolated_nested_pytest_env(tmp_path)
    for var in ("BUILD_COORDINATOR_DATABASE_URL", "BUILD_COORDINATOR_CONFIG", "BUILD_COORDINATOR_REPO_ROOT"):
        env.pop(var, None)
    env["HOME"] = str(fake_home)
    env["USERPROFILE"] = str(fake_home)
    env["PYTHONPATH"] = str(REPO_ROOT)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_objective_runner_integration.py",
            f"--basetemp={basetemp}",
            "-p",
            "no:cacheprovider",
            "-q",
            "--no-header",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, (
        f"targeted objective runner test failed under redirected HOME:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    after_raw, after_tables, after_rows = _canary_snapshot(canary_db_path)
    assert after_tables == before_tables
    assert after_rows == before_rows
    assert after_raw == before_raw


def test_nested_pytest_uses_isolated_temp_storage(tmp_path: Path):
    """Nested pytest must not create or require the user-global
    pytest-of-<user> directory under the operator TEMP."""
    env, basetemp = _isolated_nested_pytest_env(tmp_path)
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probe = probe_dir / "test_temp_probe.py"
    probe.write_text(
        "import os\n"
        "import tempfile\n"
        "from pathlib import Path\n"
        "\n"
        "def test_temp_dir_is_the_isolated_subprocess_temp():\n"
        "    isolated = Path(os.environ['TEMP']).resolve()\n"
        "    assert Path(tempfile.gettempdir()).resolve() == isolated\n"
        "    assert 'pytest-of-' not in isolated.name\n"
        "\n"
        "def test_tmp_path_lives_under_isolated_basetemp(tmp_path):\n"
        "    root = Path(os.environ['NESTED_BASETEMP']).resolve()\n"
        "    assert tmp_path.resolve().is_relative_to(root)\n",
        encoding="utf-8",
    )
    env["NESTED_BASETEMP"] = str(basetemp)
    env["PYTHONPATH"] = str(REPO_ROOT)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(probe),
            f"--basetemp={basetemp}",
            "-q",
            "--no-header",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"isolated nested pytest probe failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert basetemp.exists()
    assert any(basetemp.iterdir()), "nested pytest wrote no artifacts under isolated basetemp"
