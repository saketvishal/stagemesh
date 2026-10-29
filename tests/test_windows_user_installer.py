from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-stagemesh.ps1"
OWNER = "StageMesh user install v1"


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows PowerShell installer")


def run_installer(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )


def paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "StageMeshInstall", tmp_path / "Programs"


def fixture_wheel(directory: Path) -> tuple[Path, str]:
    wheel = directory / "stagemesh-99.0.0-py3-none-any.whl"
    files = {
        "stagemesh/__init__.py": '__version__ = "99.0.0"\n',
        "stagemesh/cli.py": 'def main():\n    print("stagemesh 99.0.0")\n    return 0\n',
        "stagemesh-99.0.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: stagemesh\nVersion: 99.0.0\n",
        "stagemesh-99.0.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: StageMesh test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        "stagemesh-99.0.0.dist-info/entry_points.txt": "[console_scripts]\nstagemesh = stagemesh.cli:main\n",
    }
    files["stagemesh-99.0.0.dist-info/RECORD"] = "".join(f"{name},,\n" for name in files) + "stagemesh-99.0.0.dist-info/RECORD,,\n"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


def test_plan_is_deterministic_per_user_and_has_no_side_effects(tmp_path: Path) -> None:
    install, programs = paths(tmp_path)
    args = ("-Action", "Plan", "-InstallRoot", str(install), "-ProgramsRoot", str(programs), "-Json")
    first, second = run_installer(*args), run_installer(*args)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    one, two = json.loads(first.stdout), json.loads(second.stdout)
    assert one == two
    assert one["per_user"] is True and one["requires_admin"] is False
    assert one["package_source"].endswith("@b6023f6733bb090ff75a65a47808660afb0a1bbd")
    assert Path(one["project"]).resolve() == ROOT.resolve()
    assert one["runtime"] == two["runtime"] and one["shortcut"].endswith("StageMesh.lnk")
    assert not install.exists() and not programs.exists()


def test_unpinned_git_source_is_rejected_before_writing(tmp_path: Path) -> None:
    install, programs = paths(tmp_path)
    result = run_installer(
        "-Action", "Plan", "-PackageSource", "stagemesh @ git+https://github.com/saketvishal/stagemesh.git@main",
        "-InstallRoot", str(install), "-ProgramsRoot", str(programs),
    )
    assert result.returncode != 0 and "must pin" in result.stderr
    assert not install.exists() and not programs.exists()


def test_uninstall_removes_owned_runtime_but_preserves_an_unverified_shortcut(tmp_path: Path) -> None:
    install, programs = paths(tmp_path)
    install.mkdir(); programs.mkdir()
    (install / ".stagemesh-user-install").write_text(OWNER, encoding="utf-8")
    (install / "install.json").write_text("{}", encoding="utf-8")
    shortcut = programs / "StageMesh.lnk"
    shortcut.write_bytes(b"fixture shortcut")
    result = run_installer(
        "-Action", "Uninstall", "-InstallRoot", str(install), "-ProgramsRoot", str(programs), "-Json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "removed"
    assert not install.exists() and shortcut.read_bytes() == b"fixture shortcut" and programs.is_dir()


def test_uninstall_refuses_an_unowned_directory(tmp_path: Path) -> None:
    install, programs = paths(tmp_path)
    install.mkdir()
    sentinel = install / "keep.txt"
    sentinel.write_text("not StageMesh", encoding="utf-8")
    result = run_installer(
        "-Action", "Uninstall", "-InstallRoot", str(install), "-ProgramsRoot", str(programs),
    )
    assert result.returncode != 0 and "ownership marker" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "not StageMesh"


def test_install_refuses_to_claim_an_existing_unowned_root_before_running_python(tmp_path: Path) -> None:
    install, programs = paths(tmp_path)
    install.mkdir()
    sentinel = install / "foreign.txt"
    sentinel.write_text("keep", encoding="utf-8")
    result = run_installer(
        "-Action", "Install", "-InstallRoot", str(install), "-ProgramsRoot", str(programs),
        "-PythonExecutable", "this-command-must-not-run.exe",
    )
    assert result.returncode != 0 and "not owned" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not programs.exists()


def test_offline_wheel_install_reuse_shortcut_and_uninstall_use_the_final_runtime(tmp_path: Path) -> None:
    install, programs = paths(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    wheel, digest = fixture_wheel(tmp_path)
    source = f"stagemesh @ {wheel.as_uri()}"
    args = (
        "-Action", "Install", "-PackageSource", source, "-WheelSha256", digest,
        "-InstallRoot", str(install), "-ProgramsRoot", str(programs), "-Project", str(project),
        "-PythonExecutable", sys.executable, "-Json",
    )

    first = run_installer(*args)
    assert first.returncode == 0, first.stdout + first.stderr
    payload = json.loads(first.stdout)
    runtime = Path(payload["runtime"])
    shortcut = programs / "StageMesh.lnk"
    assert runtime.is_dir() and (runtime / "Scripts" / "stagemesh.exe").is_file()
    assert (runtime / ".stagemesh-user-install").read_text(encoding="utf-8-sig").strip() == OWNER
    assert shortcut.is_file() and ".installing-" not in str(runtime)

    inspect = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SHORTCUT_UNDER_TEST); "
            "[pscustomobject]@{target=$s.TargetPath;arguments=$s.Arguments;working=$s.WorkingDirectory}|ConvertTo-Json -Compress",
        ],
        text=True, capture_output=True, check=False, timeout=30,
        env={**os.environ, "SHORTCUT_UNDER_TEST": str(shortcut)},
    )
    assert inspect.returncode == 0, inspect.stderr
    link = json.loads(inspect.stdout)
    assert Path(link["target"]).resolve() == (runtime / "Scripts" / "stagemesh.exe").resolve()
    assert str(project) in link["arguments"] and link["arguments"].endswith(" ui")
    assert Path(link["working"]).resolve() == project.resolve()

    second = run_installer(*args)
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(second.stdout)["runtime"] == str(runtime)
    assert [path for path in (install / "runtimes").iterdir() if path.is_dir()] == [runtime]

    removed = run_installer(
        "-Action", "Uninstall", "-InstallRoot", str(install), "-ProgramsRoot", str(programs),
        "-Project", str(project), "-Json",
    )
    assert removed.returncode == 0, removed.stderr
    assert not install.exists() and not shortcut.exists()


def test_uninstall_uses_recorded_shortcut_path_and_preserves_caller_supplied_foreign_link(tmp_path: Path) -> None:
    install, supplied_programs = paths(tmp_path)
    recorded_programs = tmp_path / "RecordedPrograms"
    install.mkdir(); supplied_programs.mkdir(); recorded_programs.mkdir()
    (install / ".stagemesh-user-install").write_text(OWNER, encoding="utf-8")
    foreign = supplied_programs / "StageMesh.lnk"
    foreign.write_bytes(b"foreign shortcut")
    recorded = recorded_programs / "StageMesh.lnk"
    recorded.write_bytes(b"changed shortcut")
    (install / "install.json").write_text(
        json.dumps(
            {
                "shortcut": str(recorded),
                "target": str(tmp_path / "missing.exe"),
                "arguments": "ui",
                "working_directory": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )

    result = run_installer(
        "-Action", "Uninstall", "-InstallRoot", str(install), "-ProgramsRoot", str(supplied_programs), "-Json",
    )
    assert result.returncode == 0, result.stderr
    assert foreign.read_bytes() == b"foreign shortcut"
    assert recorded.read_bytes() == b"changed shortcut"
    assert not install.exists()
