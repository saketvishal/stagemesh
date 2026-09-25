"""Deterministic, runner-owned integration (the SCM_OPERATOR role).

Integration of an already reviewed commit is mechanical, so it does not need a
model: merge the reviewed SHA into the target branch with a merge commit,
advance that branch safely, and (only when the project configures an
upstream) push. Every failure is a typed, actionable result instead of a hang.

Safety rules:
  * the merge happens in StageMesh's own integration worktree, never in a
    human's checkout;
  * the target branch is advanced with a compare-and-swap `update-ref`; if a
    human has that branch checked out it is only fast-forwarded when their
    checkout is clean, otherwise integration stops with a human action;
  * nothing is pushed unless the project enabled upstream push, and a push
    failure leaves the merge durable locally and the task retryable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from build_coordinator.execution.base import (
    ExecutionHandle,
    ExecutionLaunch,
    ExecutionObservation,
)
from build_coordinator.execution.results import RESULT_SCHEMA_VERSION, load_result_file
from build_coordinator.runner.git_safety import resolve_git_identity_args


class IntegrationStop(Exception):
    def __init__(self, escalation: str, detail: str, **facts: Any) -> None:
        super().__init__(detail)
        self.escalation = escalation
        self.detail = detail
        self.facts = facts


def _git(cwd: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _out(cwd: str | Path, *args: str) -> str:
    proc = _git(cwd, *args)
    if proc.returncode != 0:
        raise IntegrationStop(
            "COORDINATOR_INVARIANT_FAILURE", f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}"
        )
    return proc.stdout.strip()


def _is_ancestor(cwd: str | Path, ancestor: str, descendant: str) -> bool:
    return _git(cwd, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def branch_holder(cwd: str | Path, branch: str, *, excluding: Path) -> Path | None:
    listing = _git(cwd, "worktree", "list", "--porcelain").stdout
    current: Path | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):]).resolve()
        elif line == f"branch refs/heads/{branch}" and current is not None and current != excluding.resolve():
            return current
    return None


def push_branch(cwd: str | Path, remote: str, sha: str, branch: str) -> tuple[bool, str]:
    proc = _git(cwd, "push", remote, f"{sha}:refs/heads/{branch}")
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()[-400:]


class GitIntegrationExecutor:
    adapter_name = "builtin-git"

    def __init__(
        self,
        *,
        main_ref: str = "main",
        upstream_remote: str | None = None,
        push: bool = False,
    ) -> None:
        self._main = main_ref
        self._remote = upstream_remote
        self._push = bool(push and upstream_remote)
        self._observations: dict[str, ExecutionObservation] = {}
        self._result_paths: dict[str, str] = {}

    def remember_result_path(self, execution_id: str, result_path: str | None) -> None:
        if result_path:
            self._result_paths[execution_id] = result_path

    def launch(self, launch: ExecutionLaunch) -> ExecutionHandle:
        execution_id = launch.execution_id or str(uuid4())
        base = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "execution_id": execution_id,
            "task_id": launch.task_id,
            "role": "INTEGRATION",
            "reviewed_feature_sha": launch.reviewed_feature_sha,
            "feature_sha": launch.reviewed_feature_sha,
        }
        try:
            facts = self._integrate(launch)
            payload = {**base, **facts, "status": "SUCCEEDED"}
            observation = ExecutionObservation(status="SUCCEEDED", result_data=payload, result_path=launch.result_path)
        except IntegrationStop as stop:
            payload = {
                **base,
                **stop.facts,
                "status": "HUMAN_ACTION_REQUIRED",
                "human_escalation_type": stop.escalation,
                "detail": stop.detail,
            }
            observation = ExecutionObservation(
                status="HUMAN_ACTION_REQUIRED",
                result_data=payload,
                human_escalation_type=stop.escalation,
                result_path=launch.result_path,
            )
        if launch.result_path:
            import json

            path = Path(launch.result_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            self._result_paths[execution_id] = launch.result_path
        self._observations[execution_id] = observation
        return ExecutionHandle(execution_id=execution_id, result_path=launch.result_path)

    def poll(self, execution_id: str) -> ExecutionObservation:
        remembered = self._observations.get(execution_id)
        if remembered is not None:
            return remembered
        path = self._result_paths.get(execution_id)
        if path and Path(path).is_file():
            data = load_result_file(path)
            return ExecutionObservation(
                status=str(data.get("status", "FAILED")),
                result_data=data,
                human_escalation_type=data.get("human_escalation_type"),
                result_path=path,
            )
        return ExecutionObservation(status="LOST", result_data={"reconciliation_state": "LOST"})

    def terminate(self, execution_id: str) -> ExecutionObservation:
        return ExecutionObservation(status="TERMINATED")

    def _integrate(self, launch: ExecutionLaunch) -> dict[str, Any]:
        if not launch.worktree_path or not launch.reviewed_feature_sha:
            raise IntegrationStop("COORDINATOR_INVARIANT_FAILURE", "integration needs a worktree and a reviewed SHA")
        wt = Path(launch.worktree_path)
        sha = launch.reviewed_feature_sha
        main = self._main
        _out(wt, "rev-parse", "--verify", f"{sha}^{{commit}}")
        before = _out(wt, "rev-parse", "--verify", f"refs/heads/{main}")

        if self._push:
            fetched = _git(wt, "fetch", self._remote, main)
            if fetched.returncode != 0:
                raise IntegrationStop(
                    "UPSTREAM_PUSH_FAILED",
                    f"could not synchronize with upstream {self._remote}: {(fetched.stderr or '').strip()[-300:]}",
                    push_status="FAILED",
                    current_main_sha=before,
                )
            remote_main = _out(wt, "rev-parse", "FETCH_HEAD")
            if remote_main != before:
                if _is_ancestor(wt, before, remote_main):
                    self._advance(wt, main, remote_main, before)
                    before = remote_main
                elif not _is_ancestor(wt, remote_main, before):
                    raise IntegrationStop(
                        "MERGE_CONFLICT",
                        f"local {main} and upstream {main} have diverged; a human must reconcile them",
                        current_main_sha=before,
                    )

        merge_base = _out(wt, "merge-base", before, sha)
        _out(wt, "checkout", "--detach", before)
        identity_args = resolve_git_identity_args(wt)
        merge = _git(
            wt,
            *identity_args,
            "merge", "--no-ff", "-m", f"Integrate {launch.task_id}", sha,
        )
        if merge.returncode != 0:
            _git(wt, "merge", "--abort")
            raise IntegrationStop(
                "MERGE_CONFLICT",
                f"merging the reviewed commit into {main} conflicts",
                current_main_sha=before,
                merge_base=merge_base,
            )
        merged = _out(wt, "rev-parse", "HEAD")
        self._advance(wt, main, merged, before)

        facts: dict[str, Any] = {
            "current_main_sha": before,
            "merge_base": merge_base,
            "merge_commit_sha": merged,
            "final_main_sha": merged,
            "push_status": "NOT_REQUIRED",
            "tests": [],
        }
        if self._push:
            ok, detail = push_branch(wt, self._remote, merged, main)
            if not ok:
                facts["push_status"] = "FAILED"
                raise IntegrationStop(
                    "UPSTREAM_PUSH_FAILED",
                    f"integrated locally but the push to {self._remote} failed: {detail}",
                    **facts,
                )
            facts["push_status"] = "PUSHED"
        return facts

    def _advance(self, wt: Path, branch: str, new: str, old: str) -> None:
        holder = branch_holder(wt, branch, excluding=wt)
        if holder is None:
            proc = _git(wt, "update-ref", f"refs/heads/{branch}", new, old)
            if proc.returncode != 0:
                raise IntegrationStop(
                    "BRANCH_MOVED_CONCURRENTLY",
                    f"{branch} moved while integrating; retry: {(proc.stderr or '').strip()[-200:]}",
                )
            return
        if _git(holder, "status", "--porcelain").stdout.strip():
            from build_coordinator.runner.worktree import reconcile_displaced_task_work
            reconcile_displaced_task_work(holder)
            if _git(holder, "status", "--porcelain").stdout.strip():
                raise IntegrationStop(
                    "WORKING_CHECKOUT_DIRTY",
                    f"{branch} is checked out with uncommitted changes at {holder}; commit or stash them, then retry",
                )
        proc = _git(holder, "merge", "--ff-only", new)
        if proc.returncode != 0:
            raise IntegrationStop(
                "COORDINATOR_INVARIANT_FAILURE",
                f"could not fast-forward the {branch} checkout: {(proc.stderr or '').strip()[-200:]}",
            )
