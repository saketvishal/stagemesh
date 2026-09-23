"""Exercise the actual launcher scripts (not just the equivalent `python -m`
invocation) from directories other than the repository root, on both
platforms this repo runs on:

- `bin/build-coordinator` (POSIX shell, via `bash`)
- `bin/build-coordinator.ps1` (PowerShell)
- `bin/build-coordinator.cmd` (cmd.exe, which delegates to the .ps1)

All three must resolve the repository root from their own file location and
run `python -P -m build_coordinator.cli`, so cwd never influences
which controller checkout is loaded.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "build_coordinator" / "bin"


def _find_posix_bash() -> str | None:
    """Prefer a real POSIX-path-aware bash (Git for Windows/MSYS) over a
    WSL bash.exe that may also be on PATH -- WSL bash doesn't resolve
    Windows-style `C:/...` paths, which would fail the launcher for
    environment reasons unrelated to the code under test."""
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return shutil.which("bash")


POSIX_BASH = _find_posix_bash()

_LOCATION_ENV_VARS = (
    "CAVENTRA_REPO_ROOT",
    "CAVENTRA_BUILD_CONFIG",
    "BUILD_COORDINATOR_DATA_DIR",
    "BUILD_COORDINATOR_DATABASE_URL",
    "BUILD_COORDINATOR_MAX_ACTIVE_BUILDERS",
    "BUILD_COORDINATOR_RUNNER_CONFIG",
    "PYTHONPATH",
)


def _test_interpreter_dir() -> Path:
    """Directory of the interpreter running this test, not whatever `python`
    happens to be first on the ambient PATH."""
    return Path(sys.executable).parent


def _with_test_interpreter_on_path(env: dict) -> dict:
    """Launchers invoke bare `python`. Put the test interpreter first on PATH
    so the subprocess uses the same environment that owns coordinator
    dependencies, without baking a developer-specific absolute path into
    the launchers themselves."""
    interpreter_dir = str(_test_interpreter_dir())
    current = env.get("PATH", "")
    entries = [entry for entry in current.split(os.pathsep) if entry and entry != interpreter_dir]
    env["PATH"] = os.pathsep.join([interpreter_dir, *entries])
    return env


def _clean_env(config_path: Path, *, environ: dict | None = None) -> dict:
    env = dict(os.environ if environ is None else environ)
    for var in _LOCATION_ENV_VARS:
        env.pop(var, None)
    env["BUILD_COORDINATOR_CONFIG"] = str(config_path)
    return _with_test_interpreter_on_path(env)


def _write_decoy_python(directory: Path) -> None:
    """A PATH entry named `python` that cannot import coordinator deps."""
    posix = directory / "python"
    posix.write_text(
        "#!/bin/sh\n"
        "echo \"ModuleNotFoundError: No module named 'sqlalchemy'\" >&2\n"
        "exit 1\n",
        encoding="utf-8",
        newline="\n",
    )
    posix.chmod(posix.stat().st_mode | 0o111)
    if os.name == "nt":
        cmd = (
            "@echo off\r\n"
            "echo ModuleNotFoundError: No module named 'sqlalchemy' 1>&2\r\n"
            "exit /b 1\r\n"
        )
        (directory / "python.cmd").write_text(cmd, encoding="utf-8")
        (directory / "python.bat").write_text(cmd, encoding="utf-8")


@pytest.fixture
def coordinator_workspace(tmp_path: Path):
    control_repo_root = tmp_path / "control-repo"
    control_repo_root.mkdir()
    db_path = tmp_path / "coordinator-data" / "coordinator.sqlite3"
    db_path.parent.mkdir(parents=True)
    config_path = tmp_path / "build-coordinator.json"
    config_path.write_text(
        json.dumps(
            {
                "control_repo_root": str(control_repo_root),
                "database_url": f"sqlite:///{db_path.as_posix()}",
                "data_dir": str(db_path.parent),
            }
        ),
        encoding="utf-8",
    )
    unrelated = tmp_path / "totally-unrelated-cwd"
    unrelated.mkdir()
    return {"control_repo_root": control_repo_root, "config_path": config_path, "cwd": unrelated}


def _assert_status_ok(result: subprocess.CompletedProcess, control_repo_root: Path) -> None:
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    payload = json.loads(result.stdout)
    assert payload["workspace"]["control_repo_root"] == str(control_repo_root.resolve())
    assert payload["controller_source"]["package_root"] == str(
        (REPO_ROOT / "build_coordinator").resolve()
    )


@pytest.mark.skipif(POSIX_BASH is None, reason="bash not on PATH")
def test_posix_launcher_runs_from_unrelated_directory(coordinator_workspace):
    result = subprocess.run(
        [POSIX_BASH, str(BIN_DIR / "build-coordinator").replace("\\", "/"), "objective", "status"],
        cwd=str(coordinator_workspace["cwd"]),
        env=_clean_env(coordinator_workspace["config_path"]),
        capture_output=True,
        text=True,
        timeout=60,
    )
    _assert_status_ok(result, coordinator_workspace["control_repo_root"])


@pytest.mark.skipif(os.name != "nt", reason="PowerShell launcher is Windows-specific")
def test_powershell_launcher_runs_from_unrelated_directory(coordinator_workspace):
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BIN_DIR / "build-coordinator.ps1"),
            "objective",
            "status",
        ],
        cwd=str(coordinator_workspace["cwd"]),
        env=_clean_env(coordinator_workspace["config_path"]),
        capture_output=True,
        text=True,
        timeout=60,
    )
    _assert_status_ok(result, coordinator_workspace["control_repo_root"])


@pytest.mark.skipif(os.name != "nt", reason="cmd launcher is Windows-specific")
def test_cmd_launcher_runs_from_unrelated_directory(coordinator_workspace):
    result = subprocess.run(
        [str(BIN_DIR / "build-coordinator.cmd"), "objective", "status"],
        cwd=str(coordinator_workspace["cwd"]),
        env=_clean_env(coordinator_workspace["config_path"]),
        capture_output=True,
        text=True,
        timeout=60,
        shell=True,
    )
    _assert_status_ok(result, coordinator_workspace["control_repo_root"])


@pytest.mark.skipif(POSIX_BASH is None, reason="bash not on PATH")
def test_posix_launcher_is_not_shadowed_by_stale_cwd_checkout(coordinator_workspace):
    """The launcher itself (not just the raw `python -P -m` invocation) must
    resist a stale tooling/build_coordinator/ sitting in cwd."""
    cwd = coordinator_workspace["cwd"]
    stale_tooling = cwd / "tooling"
    stale_tooling.mkdir()
    (stale_tooling / "__init__.py").write_text("", encoding="utf-8")
    stale_build_coordinator = stale_tooling / "build_coordinator"
    stale_build_coordinator.mkdir()
    (stale_build_coordinator / "__init__.py").write_text("", encoding="utf-8")
    (stale_build_coordinator / "cli.py").write_text(
        'import json\n\n\ndef main() -> None:\n    print(json.dumps({"poison": True}))\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [POSIX_BASH, str(BIN_DIR / "build-coordinator").replace("\\", "/"), "objective", "status"],
        cwd=str(cwd),
        env=_clean_env(coordinator_workspace["config_path"]),
        capture_output=True,
        text=True,
        timeout=60,
    )
    _assert_status_ok(result, coordinator_workspace["control_repo_root"])
    assert "poison" not in result.stdout


def test_launcher_env_does_not_hardcode_a_developer_interpreter_path():
    """The test harness must inherit sys.executable, never a machine-local path."""
    source = Path(__file__).read_text(encoding="utf-8")
    assert "sys.executable" in source
    assert "_test_interpreter_dir" in source
    assert "_with_test_interpreter_on_path" in source
    for launcher in (
        BIN_DIR / "build-coordinator",
        BIN_DIR / "build-coordinator.ps1",
        BIN_DIR / "build-coordinator.cmd",
    ):
        text = launcher.read_text(encoding="utf-8")
        assert "sys.executable" not in text
        assert "python.exe" not in text.lower()
        assert "Program Files" not in text


def test_launchers_still_invoke_python_with_safe_path_flag():
    posix = (BIN_DIR / "build-coordinator").read_text(encoding="utf-8")
    ps1 = (BIN_DIR / "build-coordinator.ps1").read_text(encoding="utf-8")
    assert "python -P -m build_coordinator.cli" in posix
    assert "python -P -m build_coordinator.cli" in ps1


def test_launcher_subprocess_environment_can_import_coordinator_dependencies(
    coordinator_workspace,
):
    env = _clean_env(coordinator_workspace["config_path"])
    result = subprocess.run(
        [sys.executable, "-c", "import sqlalchemy, sys; print(sys.executable)"],
        cwd=str(coordinator_workspace["cwd"]),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == Path(sys.executable).resolve()


def test_launcher_prefers_test_interpreter_over_decoy_path_python(
    coordinator_workspace, tmp_path
):
    decoy = tmp_path / "decoy-python"
    decoy.mkdir()
    _write_decoy_python(decoy)
    hostile = dict(os.environ)
    hostile["PATH"] = str(decoy) + os.pathsep + hostile.get("PATH", "")
    env = _clean_env(coordinator_workspace["config_path"], environ=hostile)
    path_head = Path(env["PATH"].split(os.pathsep)[0])
    assert path_head == _test_interpreter_dir()
    assert path_head != decoy

    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BIN_DIR / "build-coordinator.ps1"),
            "objective",
            "status",
        ]
    else:
        if POSIX_BASH is None:
            pytest.skip("bash not on PATH")
        command = [
            POSIX_BASH,
            str(BIN_DIR / "build-coordinator").replace("\\", "/"),
            "objective",
            "status",
        ]

    result = subprocess.run(
        command,
        cwd=str(coordinator_workspace["cwd"]),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    _assert_status_ok(result, coordinator_workspace["control_repo_root"])
    assert "poison" not in result.stdout
    assert "sqlalchemy" not in result.stderr
