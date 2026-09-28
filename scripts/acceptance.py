from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    merged = os.environ.copy()
    merged.update(env or {})
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, env=merged)
    if result.returncode != 0:
        raise AssertionError(f"{command} failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return result.stdout


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="stagemesh-acceptance-") as raw:
        project = Path(raw) / "project"
        project.mkdir()
        env = {"PYTHONPATH": str(ROOT / "src")}
        run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "init"], ROOT, env)
        objective = project / "objective.json"
        objective.write_text(
            json.dumps(
                {
                    "id": "obj-1",
                    "title": "synthetic objective",
                    "tasks": [
                        {"id": "one", "title": "first task"},
                        {"id": "two", "title": "dependent task", "dependencies": ["one"]},
                        {"id": "deferred", "title": "future work", "eligible": False},
                    ],
                }
            ),
            encoding="utf-8",
        )
        run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "plan", str(objective)], ROOT, env)
        backlog = project / ".stagemesh" / "backlog.json"
        for _ in range(8):
            run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "continue", "--once"], ROOT, env)
        status = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "status"], ROOT, env)
        if "DONE DONE first task" not in status or "DONE DONE dependent task" not in status:
            raise AssertionError(status)
        if "future work" in status:
            raise AssertionError("deferred task was dispatched")
        doctor = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "doctor"], ROOT, env)
        required = ["version:", "executable path:", "python interpreter:", "imported package path:", "db:"]
        missing = [item for item in required if item not in doctor]
        if missing:
            raise AssertionError(f"doctor missing {missing}")
        health = run([sys.executable, "-m", "stagemesh.cli", "--project", str(project), "health"], ROOT, env)
        if "ok: True" not in health or "done: 2" not in health:
            raise AssertionError(health)
        capacity = run(
            [
                sys.executable,
                "-m",
                "stagemesh.cli",
                "--project",
                str(project),
                "capacity",
                "--primary-down",
            ],
            ROOT,
            env,
        )
        if "chosen: claude" not in capacity:
            raise AssertionError(capacity)
        shutil.rmtree(project / ".git", ignore_errors=True)
    print("acceptance: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
