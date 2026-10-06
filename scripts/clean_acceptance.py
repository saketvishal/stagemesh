from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stagemesh.persistence import SCHEMA_VERSION


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    merged = os.environ.copy()
    merged.update(env or {})
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, env=merged, check=False)
    if result.returncode != 0:
        raise AssertionError(f"{command} failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return result.stdout


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return [Path(raw.decode("utf-8")) for raw in result.stdout.split(b"\0") if raw]


def copy_clean_tree(source: Path, destination: Path) -> None:
    for relative in tracked_files(source):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="stagemesh-clean-acceptance-") as raw:
        tmp = Path(raw)
        clean_root = tmp / "checkout"
        install_target = tmp / "install"
        project = tmp / "synthetic-project"
        clean_root.mkdir()
        project.mkdir()
        copy_clean_tree(ROOT, clean_root)
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                ".",
                "--target",
                str(install_target),
                "--no-cache-dir",
                "--upgrade",
            ],
            clean_root,
        )
        env = {"PYTHONPATH": str(install_target)}
        init = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "init"], clean_root, env)
        if "initialized StageMesh" not in init:
            raise AssertionError(init)
        doctor = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "doctor"], clean_root, env)
        required = [
            "version:",
            "executable path:",
            "imported package path:",
            f"project: {project.resolve()}",
            f"schema version: {SCHEMA_VERSION}",
            "backend: sqlite",
        ]
        missing = [item for item in required if item not in doctor]
        if missing:
            raise AssertionError(f"doctor missing {missing}\n{doctor}")
        backend = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "backend"], clean_root, env)
        if "name: sqlite" not in backend or "postgres schema contract: 18 tables" not in backend:
            raise AssertionError(backend)
        report = project / ".stagemesh" / "final-report.md"
        report_output = run(
            [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "report", "--output", str(report)],
            clean_root,
            env,
        )
        report_text = report.read_text(encoding="utf-8") if report.exists() else ""
        if "report:" not in report_output or "## Final Architecture" not in report_text:
            raise AssertionError(report_output + report_text)
    print("clean_acceptance: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
