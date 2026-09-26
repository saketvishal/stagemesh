"""Real coding-agent CLI subprocess for the INTEGRATION role (SM-012).

Used only by the live scratch-repo scenario in
``tests/test_real_agent_end_to_end.py``, via a ``workers.integration``
project.yaml template with ``adapter: subprocess``. This is deliberately NOT
``build_coordinator/agents/wrapper.py``: that module never special-cases the
INTEGRATION role (``role_key`` is only remapped for REMEDIATION), so its
generic builder prompt tells the agent not to touch git history at all and
its result derivation reports ``feature_sha`` from HEAD, never a
``merge_commit_sha``. Editing ``wrapper.py``/``orchestrator.py`` is out of
this task's allowed paths (``examples/``, ``docs/``, ``tests/`` only), so
this script is a standalone, INTEGRATION-aware subprocess adapter target:
it drives the real ``claude`` CLI headlessly with permission to run
``git checkout``/``git merge``, then -- following the same discipline as
``wrapper.py`` ("an agent's self-report is never the lifecycle result") --
independently verifies from git state whether the merge the agent was asked
to perform actually happened before ever writing ``status: SUCCEEDED``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

RESULT_SCHEMA_VERSION = 1


def _git(cwd: str, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main() -> int:
    cwd = os.getcwd()
    role = os.environ.get("BUILD_COORDINATOR_ROLE", "").upper()
    result_path = Path(os.environ["BUILD_COORDINATOR_RESULT_PATH"])
    identity = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "execution_id": os.environ["BUILD_COORDINATOR_EXECUTION_ID"],
        "task_id": os.environ["BUILD_COORDINATOR_TASK_ID"],
        "role": role,
    }
    reviewed_sha = os.environ.get("BUILD_COORDINATOR_REVIEWED_FEATURE_SHA") or None
    main_ref = os.environ.get("STAGEMESH_MAIN_REF", "main")
    # Prompt is delivered on stdin, matching the SubprocessExecutor contract
    # every other worker command uses; unused here beyond draining the pipe.
    sys.stdin.read()

    def fail(detail: str, **extra: object) -> int:
        payload = {**identity, "status": "FAILED", "detail": detail, **extra}
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        return 1

    if role != "INTEGRATION" or not reviewed_sha:
        return fail("integration worker requires BUILD_COORDINATOR_ROLE=INTEGRATION and a reviewed feature sha")

    exe = shutil.which("claude")
    if exe is None:
        return fail("claude CLI not found on PATH")

    rc, before, err = _git(cwd, "rev-parse", "--verify", main_ref)
    if rc != 0:
        return fail(f"could not resolve {main_ref} before merge: {err}")

    rc, merge_base, err = _git(cwd, "merge-base", before, reviewed_sha)
    if rc != 0:
        return fail(f"could not compute merge-base of {before} and {reviewed_sha}: {err}")

    allowed_tools = (
        "Read,Grep,Glob,"
        "Bash(git status:*),Bash(git log:*),Bash(git diff:*),Bash(git show:*),"
        "Bash(git checkout:*),Bash(git merge:*),Bash(git rev-parse:*)"
    )
    agent_prompt = (
        "You are StageMesh's integration operator working in a real git worktree. "
        f"A reviewed commit {reviewed_sha} must be merged into the '{main_ref}' branch "
        "of this repository.\n\n"
        "Run exactly these real git commands, in order, in this worktree:\n"
        f"  git checkout {main_ref}\n"
        f"  git merge --no-ff -m \"Integrate {identity['task_id']}\" {reviewed_sha}\n\n"
        "Do not resolve conflicts creatively, do not touch any other branch, and do not "
        "push anywhere. After the merge succeeds, reply with exactly one line: "
        "MERGE_DONE"
    )
    cmd = [
        exe,
        "-p",
        "--output-format",
        "text",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        allowed_tools,
    ]
    timeout = float(os.environ.get("STAGEMESH_AGENT_TIMEOUT", "600"))
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            input=agent_prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return fail(f"claude CLI timed out after {timeout}s", current_main_sha=before)

    if proc.returncode != 0:
        return fail(
            f"claude CLI exited {proc.returncode}: {(proc.stdout + proc.stderr)[-500:]}",
            current_main_sha=before,
        )

    rc, after, err = _git(cwd, "rev-parse", "--verify", main_ref)
    if rc != 0:
        return fail(f"could not resolve {main_ref} after agent run: {err}", current_main_sha=before)
    if after == before:
        return fail("agent did not advance the main ref", current_main_sha=before)

    rc, branch, err = _git(cwd, "symbolic-ref", "--short", "HEAD")
    if rc != 0 or branch != main_ref:
        return fail(
            f"worktree HEAD is not on {main_ref} after the agent run (got {branch!r})",
            current_main_sha=before,
        )

    rc, parents, err = _git(cwd, "log", "-1", "--format=%P", after)
    parent_shas = parents.split()
    if reviewed_sha not in parent_shas or before not in parent_shas:
        return fail(
            f"resulting commit {after} is not a merge of {before} and {reviewed_sha}",
            current_main_sha=before,
            merge_base=merge_base,
        )

    payload = {
        **identity,
        "status": "SUCCEEDED",
        "reviewed_feature_sha": reviewed_sha,
        "feature_sha": reviewed_sha,
        "current_main_sha": before,
        "merge_base": merge_base,
        "merge_commit_sha": after,
        "final_main_sha": after,
        "push_status": "NOT_REQUIRED",
        "tests": [],
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
