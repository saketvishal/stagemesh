#!/usr/bin/env python
"""Public, reproducible failure-recovery demo for StageMesh.

Proves the lifecycle:

  Task starts with worker/provider A
  -> worker process is deliberately interrupted (crashes)
  -> StageMesh preserves authoritative work state/checkpoint
  -> another eligible worker resumes the task
  -> deterministic validation runs
  -> independent review targets the exact reviewed SHA
  -> the task reaches governed completion (DONE)

This script sets up a throwaway git project in a temp directory, defines one
task, and runs the real `build_coordinator` CLI (`python -m build_coordinator
continue <project>`) against it -- the same code path a real StageMesh
deployment uses, with a deterministic scripted worker standing in for a real
coding-agent CLI so the demo is fast, offline, and reproducible.

The worker is instructed (via SCRIPTED_WORKER_CRASH) to exit without a
result on its first attempt for the demo task, simulating a crashed
worker/provider. StageMesh's own crash detection and recovery -- not this
script -- is what resumes the task.

Run it:

    python docs/evidence/failure_recovery_demo.py

It prints the real CLI JSON output and a short human-readable summary of
the execution/event evidence, and writes a full transcript to
docs/evidence/failure_recovery_demo_output.json next to this script.

No network access, no real AI provider credentials, and no Caventra or
private product code are used or required.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTED_WORKER = REPO_ROOT / "tests" / "_scripted_worker.py"
OUTPUT_PATH = Path(__file__).resolve().parent / "failure_recovery_demo_output.json"

TASK_ID = "DEMO-1"


def run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(args)}\n{result.stderr}")
    return result


def setup_project(root: Path) -> Path:
    origin = root / "origin.git"
    run(root, "git", "init", "--bare", "-b", "main", str(origin))
    project = root / "project"
    project.mkdir()
    run(project, "git", "init", "-b", "main")
    for key, value in {
        "user.name": "StageMesh Demo",
        "user.email": "demo@stagemesh.invalid",
    }.items():
        run(project, "git", "config", key, value)

    (project / ".stagemesh" / "tasks").mkdir(parents=True)
    worker = {
        "provider": "scripted",
        "adapter": "subprocess",
        "command": [sys.executable, "-P", str(SCRIPTED_WORKER)],
        "timeout_seconds": 60,
    }
    project_yaml = {
        "schema_version": 1,
        "id": "demo",
        "name": "Failure Recovery Demo",
        "aliases": ["demo"],
        "execution": {"concurrency": 1, "reviewers": 1, "default_review_policy": "INDEPENDENT"},
        "workers": {"builder": worker, "reviewer": worker},
        "upstream": {"remote": "origin", "push": True},
    }
    import yaml  # local import: only needed for this demo script

    (project / ".stagemesh" / "project.yaml").write_text(yaml.safe_dump(project_yaml), encoding="utf-8")
    backlog = {
        "tasks": [
            {
                "id": TASK_ID,
                "title": "Demo task: prove crash recovery",
                "objective": "Exercise the failure-recovery lifecycle deterministically.",
                "acceptance_criteria": ["Task reaches DONE despite a first-attempt worker crash."],
            }
        ],
    }
    (project / ".stagemesh" / "tasks" / "backlog.yaml").write_text(yaml.safe_dump(backlog), encoding="utf-8")
    (project / ".gitignore").write_text(".build-coordinator/\n", encoding="utf-8")
    (project / "README.md").write_text("StageMesh failure-recovery demo fixture.\n", encoding="utf-8")
    run(project, "git", "add", ".")
    run(project, "git", "commit", "-m", "init")
    run(project, "git", "remote", "add", "origin", str(origin))
    run(project, "git", "push", "origin", "main")
    return project


def run_demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="stagemesh-demo-") as tmp:
        tmp_path = Path(tmp)
        project = setup_project(tmp_path)
        registry = tmp_path / "registry.json"

        env = {k: v for k, v in os.environ.items()}
        env.update(
            {
                "PYTHONPATH": str(REPO_ROOT),
                "STAGEMESH_PROJECT_REGISTRY": str(registry),
                "STAGEMESH_POLL_SECONDS": "0.2",
                "BUILD_COORDINATOR_AUTO_PUSH_ALLOWED": "true",
                "SCRIPTED_WORKER_DELAY": "0.5",
                # This is the deliberate interruption: the demo task's first
                # builder attempt exits without producing a result, exactly
                # as a crashed real worker/provider process would.
                "SCRIPTED_WORKER_CRASH": TASK_ID,
            }
        )

        register = subprocess.run(
            [sys.executable, "-P", "-m", "build_coordinator", "project", "register", str(project)],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
        )
        if register.returncode != 0:
            raise SystemExit(f"project register failed: {register.stderr}")

        result = subprocess.run(
            [sys.executable, "-P", "-m", "build_coordinator", "continue", "demo"],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise SystemExit(f"stagemesh continue failed: {result.stderr}")

        payload = json.loads(result.stdout)
        return payload


def summarize(payload: dict) -> str:
    final_tasks = {t["task_id"]: t["state"] for t in payload["final"]["tasks"]}
    executions = payload["final"]["executions"]
    lines = ["", "=== Failure-recovery demo summary ===", ""]
    lines.append(f"Final task state: {final_tasks.get(TASK_ID, 'UNKNOWN')}")
    lines.append("")
    lines.append("Execution history for the demo task:")
    for execution in executions:
        if execution["task_id"] != TASK_ID:
            continue
        lines.append(f"  - role={execution['role']:<9} status={execution['status']}")
    lines.append("")
    crashed = any(e for e in executions if e["task_id"] == TASK_ID and e["status"] == "LOST")
    recovered = final_tasks.get(TASK_ID) == "DONE"
    lines.append(f"Crash observed and recorded (status=LOST): {crashed}")
    lines.append(f"Task reached governed completion (DONE) after recovery: {recovered}")
    return "\n".join(lines)


def main() -> int:
    payload = run_demo()
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(summarize(payload))
    print(f"\nFull transcript written to: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
