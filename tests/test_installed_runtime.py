"""StageMesh runs as an independently installed runtime: a host project never depends on this source checkout."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1]


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


def _install(venv: Path) -> None:
    if not venv.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # the build backend has no build requirements, so this works offline
    subprocess.run(
        [str(py), "-m", "pip", "install", "--force-reinstall", "--no-deps", "--no-build-isolation", "--no-cache-dir", str(SOURCE)],
        check=True,
        capture_output=True,
    )


def _cli(venv: Path) -> Path:
    return venv / ("Scripts/stagemesh.exe" if os.name == "nt" else "bin/stagemesh")


def _env(**extra: str) -> dict[str, str]:
    # no PYTHONPATH: the checkout is not importable, and a PYTHONPATH pointing at a vanished checkout must not matter either
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(extra)
    return env


def _run(venv: Path, project: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_cli(venv)), "--project", str(project), *args],
        cwd=project,
        env=env or _env(),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture(scope="module")
def venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("runtime") / "venv"
    _install(path)
    return path


@pytest.fixture()
def project(tmp_path: Path, venv: Path) -> Path:
    proj = tmp_path / "host"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    assert _run(venv, proj, "init").returncode == 0
    return proj


def _runtime_info(venv: Path, project: Path, env: dict[str, str] | None = None) -> dict:
    proc = _run(venv, project, "doctor", "--no-network", "--json", env=env)
    assert proc.stdout, proc.stderr
    return json.loads(proc.stdout)["runtime_info"]


def test_installed_runtime_runs_without_source_on_pythonpath(venv: Path, project: Path) -> None:
    proc = subprocess.run([str(_cli(venv)), "--version"], cwd=project, env=_env(), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("stagemesh ")
    assert "stagemesh runtime: installed" in proc.stdout
    info = _runtime_info(venv, project)
    assert not info["source_checkout"] and not info["editable"]
    assert Path(info["package_path"]).is_relative_to(venv)


def test_runtime_survives_unavailable_source_checkout(venv: Path, project: Path, tmp_path: Path) -> None:
    gone = tmp_path / "moved-away" / "src"  # where a source checkout used to be: nothing there now
    info = _runtime_info(venv, project, env=_env(PYTHONPATH=str(gone)))
    assert info["mode"] == "installed"
    assert not str(SOURCE) in info["package_path"]


def test_doctor_reports_independent_runtime_and_project_state(venv: Path, project: Path) -> None:
    proc = _run(venv, project, "doctor", "--no-network")
    assert proc.returncode in (0, 1), proc.stderr
    out = proc.stdout
    assert "stagemesh runtime: installed" in out
    assert "editable install: no" in out
    assert "source checkout: no" in out
    assert "editable/development status: installed" in out
    assert str(venv) in out
    assert str(SOURCE / "src") not in out
    assert "schema version:" in out and str(project / ".stagemesh") in out  # the project's own state is recognised


def test_runtime_environment_has_no_editable_pth(venv: Path) -> None:
    site = next(venv.glob("Lib/site-packages")) if os.name == "nt" else next(venv.glob("lib/python*/site-packages"))
    for pth in site.glob("*.pth"):
        assert str(SOURCE) not in pth.read_text(encoding="utf-8", errors="replace"), pth
    assert not list(site.glob("__editable__*"))
    direct = list(site.glob("stagemesh-*.dist-info/direct_url.json"))
    assert not any(json.loads(p.read_text(encoding="utf-8")).get("dir_info", {}).get("editable") for p in direct)


def test_existing_project_state_stays_readable_across_reinstall(venv: Path, project: Path) -> None:
    before = _tree_digest(project)
    assert before, "init should have written project state"
    _install(venv)  # an update of the runtime
    after = _tree_digest(project)
    assert after == before  # reinstalling StageMesh does not touch the host project's files
    proc = _run(venv, project, "doctor", "--no-network")
    assert "schema version:" in proc.stdout


def test_run_window_plans_against_installed_runtime(venv: Path, project: Path) -> None:
    proc = _run(venv, project, "run-window", "--plan", "--no-progress")
    assert "Traceback" not in proc.stderr, proc.stderr
    assert "invalid choice" not in proc.stderr
    assert "runtime:" in proc.stdout or "would run" in proc.stdout


def test_source_runtime_is_reported_as_development() -> None:
    from stagemesh.runtime_info import runtime_info

    info = runtime_info()
    if "site-packages" not in info["package_path"]:
        assert info["source_checkout"] and info["mode"].startswith("development")


@pytest.mark.skipif(os.name != "nt", reason="cmd shim")
def test_project_shim_ignores_pythonpath(venv: Path, project: Path) -> None:
    spec = importlib.util.spec_from_file_location("bootstrap_stagemesh", SOURCE / "scripts" / "bootstrap_stagemesh.py")
    assert spec and spec.loader
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    shim = project / "shim.cmd"
    shim.write_text(boot._windows_shim(_cli(venv)), encoding="utf-8")
    proc = subprocess.run(
        ["cmd", "/c", str(shim), "--version"], cwd=project, env=_env(PYTHONPATH=str(SOURCE / "src")), capture_output=True, text=True
    )
    assert "stagemesh runtime: installed" in proc.stdout, proc.stdout + proc.stderr
