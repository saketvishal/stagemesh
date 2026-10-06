"""Integration for parallel runs: one task at a time moves the integration ref, and a stale candidate is rebased or typed."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from .audit import record_audit
from .concurrency import IntegrationLock
from .contract_binding import contract_for_candidate
from .contracts import ContractError
from .domain import EvidenceStatus, ExecutionKind, Stage
from .git import GitError, GitWorkspace
from .integration import Integrator
from .persistence import Store
from .workspace_guard import owned_workspace

REBASED_EVENT = "integration.rebased"
STALE_BASE = "integration_stale_base"  # the ref advanced and the rebase budget is spent
REBASE_CONFLICT = "integration_rebase_conflict"  # the ref advanced and the candidate no longer applies cleanly


class SerializedIntegrator(Integrator):
    """Holds the integration lock around the whole integrate step, so verification and the ref update see the same ref.

    When the ref advanced past the candidate's base (another task landed first) it rebases the candidate in the task's own
    worktree under the lock, binds the rebased commit to the same contract with the new tip as baseline, and sends the task
    back to VALIDATE: the rebased tree is different code and must be validated and reviewed again before it can land.
    A conflicting or over-budget rebase leaves INTEGRATION evidence FAILED with a typed finding instead of retrying forever.
    """

    def __init__(
        self,
        integration_ref: str | None,
        require_independent_review: bool,
        lock: IntegrationLock,
        max_rebases: int = 2,
        on_event: Callable[[str, str, dict[str, object]], None] | None = None,
    ):
        super().__init__(integration_ref, require_independent_review)
        self.lock = lock
        self.max_rebases = max_rebases
        self.on_event = on_event
        self._typed_failure: tuple[str, str] | None = None

    def integrate(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> EvidenceStatus | None:  # type: ignore[override]
        with self.lock.hold(task_id):
            self._typed_failure = None
            if self.integration_ref is not None and self._diverged(project, candidate_sha) is not None:
                if self._rebase(store, task_id, candidate_sha, project):
                    return None  # sent back to VALIDATE with a rebased candidate
            try:
                return super().integrate(store, task_id, candidate_sha, project)
            finally:
                self._typed_failure = None

    def _resolve(self, project: Path) -> str | None:
        result = GitWorkspace(project).run("rev-parse", "--verify", "--quiet", f"{self.integration_ref}^{{commit}}", check=False)
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None

    def _diverged(self, project: Path, candidate_sha: str) -> str | None:
        """The ref tip when it is neither an ancestor nor a descendant of the candidate (so a fast-forward is impossible)."""
        tip = self._resolve(project)
        if tip is None or tip == candidate_sha:
            return None
        git = GitWorkspace(project)
        if git.run("merge-base", "--is-ancestor", tip, candidate_sha, check=False).returncode == 0:
            return None
        if git.run("merge-base", "--is-ancestor", candidate_sha, tip, check=False).returncode == 0:
            return None
        return tip

    def _rebase_count(self, store: Store, task_id: str) -> int:
        count = 0
        for row in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=?", (REBASED_EVENT,)):
            try:
                if json.loads(row["payload"]).get("task_id") == task_id:
                    count += 1
            except (TypeError, ValueError):
                continue
        return count

    def _rebase(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> bool:
        tip = self._resolve(project)
        if tip is None:
            return False
        attempts = self._rebase_count(store, task_id)
        if attempts >= self.max_rebases:
            self._typed_failure = (
                STALE_BASE,
                f"{self.integration_ref} advanced to {tip} again after {attempts} automatic rebase(s) of task {task_id}; "
                "ref left unchanged. Retry the task explicitly once the ref settles.",
            )
            self._emit(task_id, STALE_BASE, {"ref_tip": tip, "rebases": attempts})
            return False
        try:
            bound = contract_for_candidate(store, task_id, candidate_sha, project)
            # The rebase rewrites the task worktree, so it runs under the workspace lease: an external edit or commit since the candidate
            # was sealed stops here (WorkspaceMutation propagates to the coordinator) instead of being reset or rebased over.
            with owned_workspace(store, project, task_id, ExecutionKind.INTEGRATION) as lease:
                worktree = lease.path
                git = GitWorkspace(worktree)
                git.run("reset", "--hard", "HEAD", check=False)
                git.run("clean", "-fdq", check=False)
                git.run("checkout", "--detach", candidate_sha)
                result = git.run("rebase", tip, check=False)
                if result.returncode != 0:
                    conflicts = [
                        line for line in git.run("diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines() if line
                    ]
                    git.run("rebase", "--abort", check=False)
                    git.run("checkout", "--detach", candidate_sha, check=False)
                    lease.seal(candidate_sha)
                    self._typed_failure = (
                        REBASE_CONFLICT,
                        f"{self.integration_ref} advanced to {tip} and candidate {candidate_sha} conflicts with it"
                        + (f" in {', '.join(conflicts[:10])}" if conflicts else "")
                        + "; ref left unchanged",
                    )
                    self._emit(task_id, REBASE_CONFLICT, {"ref_tip": tip, "conflicts": conflicts[:10]})
                    return False
                rebased = git.head()
                previous = store.latest_candidate(task_id)
                producer = str(previous["produced_by"]) if previous is not None else "rebase"
                store.add_candidate(task_id, rebased, producer, durable_handoff=True)
                lease.seal(rebased)
        except (GitError, ContractError, OSError) as exc:
            self._typed_failure = (REBASE_CONFLICT, f"could not rebase candidate {candidate_sha} onto {tip}: {exc}")
            self._emit(task_id, REBASE_CONFLICT, {"ref_tip": tip, "error": str(exc)[:200]})
            return False
        store.bind_contract(task_id, rebased, tip, bound.version, bound.digest, bound.canonical_json)
        store.advance_task(task_id, Stage.VALIDATE)
        record_audit(
            store,
            REBASED_EVENT,
            {"task_id": task_id, "from_candidate": candidate_sha, "candidate_sha": rebased, "onto": tip, "ref": str(self.integration_ref)},
        )
        self._emit(task_id, "integration_rebased", {"from_candidate": candidate_sha, "candidate_sha": rebased, "onto": tip})
        return True

    def _emit(self, task_id: str, event: str, detail: dict[str, object]) -> None:
        if self.on_event is not None:
            self.on_event(task_id, event, detail)

    def _fast_forward(self, project, candidate_sha, payload, findings):  # type: ignore[no-untyped-def]
        if self._typed_failure is not None:
            code, message = self._typed_failure
            payload["integration_ref_before"] = payload["integration_ref_after"] = self._resolve(project)
            findings.append({"severity": "error", "code": code, "message": message})
            return
        super()._fast_forward(project, candidate_sha, payload, findings)
