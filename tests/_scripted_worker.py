"""Deterministic stand-in agent CLI for exercising the real runner lifecycle.

It speaks the executor contract (prompt on stdin, JSON result file at
BUILD_COORDINATOR_RESULT_PATH) and performs real git work in the worktree
StageMesh gave it, so claims, worktree provisioning, checkpoints, review SHA
capture and integration all run through the production code paths. It is a
test/demo worker only; it never runs in a real project configuration.

Environment knobs (all optional):
  SCRIPTED_WORKER_DELAY    seconds a builder "works" (default 0.5)
  SCRIPTED_WORKER_TRACE    file that receives one JSON line per execution
  SCRIPTED_WORKER_CRASH    task id whose first builder attempt exits without a result
  SCRIPTED_WORKER_REWORK   task id whose first review returns REMEDIATION_REQUIRED
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, cwd=os.getcwd()
    )
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def trace(event: dict) -> None:
    path = os.environ.get("SCRIPTED_WORKER_TRACE")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")


def first_time(marker_name: str, task_id: str) -> bool:
    marker = Path(os.environ["BUILD_COORDINATOR_RESULT_PATH"]).parent / f"{marker_name}-{task_id}.marker"
    if marker.exists():
        return False
    marker.write_text("seen", encoding="utf-8")
    return True


def main() -> int:
    sys.stdin.read()
    role = os.environ["BUILD_COORDINATOR_ROLE"]
    task_id = os.environ["BUILD_COORDINATOR_TASK_ID"]
    execution_id = os.environ["BUILD_COORDINATOR_EXECUTION_ID"]
    result_path = Path(os.environ["BUILD_COORDINATOR_RESULT_PATH"])
    started = datetime.now(UTC).isoformat()
    base = {"schema_version": 1, "execution_id": execution_id, "task_id": task_id, "role": role}
    for key, value in {
        "GIT_AUTHOR_NAME": "Scripted Worker",
        "GIT_AUTHOR_EMAIL": "scripted-worker@example.invalid",
        "GIT_COMMITTER_NAME": "Scripted Worker",
        "GIT_COMMITTER_EMAIL": "scripted-worker@example.invalid",
    }.items():
        os.environ.setdefault(key, value)

    provider = os.environ.get("SCRIPTED_WORKER_PROVIDER", "")
    if provider and provider in os.environ.get("SCRIPTED_WORKER_FAIL_PROVIDERS", "").split(","):
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps({**base, "status": "FAILED", "provider_failure": "RATE_LIMITED", "detail": f"{provider} is rate limited"}),
            encoding="utf-8",
        )
        trace({"role": role, "task_id": task_id, "provider": provider, "outcome": "provider_failure"})
        return 1

    if role in ("BUILDER", "REMEDIATION"):
        time.sleep(float(os.environ.get("SCRIPTED_WORKER_DELAY", "0.5")))
        if os.environ.get("SCRIPTED_WORKER_CRASH") == task_id and first_time("crash", task_id):
            trace({"role": role, "task_id": task_id, "cwd": os.getcwd(), "outcome": "crashed"})
            return 3
        target = Path("stagemesh-scripted") / f"{task_id}.txt"
        target.parent.mkdir(exist_ok=True)
        target.write_text(f"work for {task_id} by execution {execution_id}\n", encoding="utf-8")
        git("add", str(target))
        if role == "REMEDIATION":
            Path("fixed.txt").write_text("fixed", encoding="utf-8")
            git("add", "fixed.txt")
        git("commit", "-m", f"scripted work for {task_id}")
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        sha = git("rev-parse", "HEAD")
        payload = {
            **base,
            "status": "SUCCEEDED",
            "feature_sha": sha,
            "files_changed": [target.as_posix()],
            "commits_created": [sha],
            "tests": ["scripted-validation: passed"],  # self-report; StageMesh must not trust it
            "scope_expansion_required": False,
            "blockers": [],
        }
    elif role == "REVIEWER":
        reviewed = os.environ.get("BUILD_COORDINATOR_REVIEWED_FEATURE_SHA", "")
        rework = os.environ.get("SCRIPTED_WORKER_REWORK") == task_id and first_time("rework", task_id)
        payload = {
            **base,
            "status": "SUCCEEDED",
            "reviewed_feature_sha": reviewed,
            "verdict": "REMEDIATION_REQUIRED" if rework else "GREEN",
            "findings": ["scripted finding"] if rework else [],
            "required_remediation": ["scripted remediation"] if rework else [],
            "architecture_notes": [],
            "ready_for_integration": not rework,
        }
    elif role == "INTEGRATION":
        reviewed = os.environ["BUILD_COORDINATOR_REVIEWED_FEATURE_SHA"]
        git("fetch", "origin", "--prune")
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        git("checkout", "-B", branch, "origin/main")
        before = git("rev-parse", "HEAD")
        git("merge", "--no-ff", "-m", f"integrate {task_id}", reviewed)
        merged = git("rev-parse", "HEAD")
        git("push", "origin", "HEAD:main")
        payload = {
            **base,
            "status": "SUCCEEDED",
            "feature_sha": reviewed,
            "reviewed_feature_sha": reviewed,
            "current_main_sha": before,
            "merge_commit_sha": merged,
            "final_main_sha": merged,
            "push_status": "PUSHED",
            "tests": ["scripted-integration: passed"],
        }
    else:
        raise SystemExit(f"unsupported role {role}")

    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    trace(
        {
            "role": role,
            "task_id": task_id,
            "cwd": os.getcwd(),
            "started": started,
            "finished": datetime.now(UTC).isoformat(),
            "outcome": payload["status"],
            "verdict": payload.get("verdict"),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
