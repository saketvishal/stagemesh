"""Integration for parallel runs: one task at a time moves the integration ref, and a stale candidate is rebased or typed."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from .audit import record_audit
from .concurrency import IntegrationLock
from .contract_binding import contract_for_candidate
from .contracts import ContractError, _matches, changed_files
from .domain import EvidenceStatus, ExecutionKind, Stage
from .git import GitError, GitWorkspace
from .integration import Integrator, _attribution_findings
from .persistence import Store
from .reattribution import reattribute_range
from .review_identity import record_implementation_derivation
from .workspace_guard import owned_workspace

REBASED_EVENT = "integration.rebased"
REATTRIBUTED_EVENT = "integration.reattributed"
RECONSTRUCTED_EVENT = "integration.reconstructed"
STALE_BASE = "integration_stale_base"  # the ref advanced and the rebase budget is spent
REBASE_CONFLICT = "integration_rebase_conflict"  # the ref advanced and the candidate no longer applies cleanly
REBASE_UNAVAILABLE = "integration_rebase_unavailable"  # git/runtime failed without a content conflict


def _preserve_original(project: Path, task_id: str, candidate_sha: str) -> None:
    """Keep the reviewed candidate reachable once a refreshed replacement exists: the ledger still names it as history."""
    from .autonomy.gitfacts import GitFacts
    from .workspaces import _task_key

    GitFacts(project).ensure_ref(f"refs/stagemesh/preserved/{_task_key(task_id)}/{candidate_sha[:12]}", candidate_sha)


def _rebase_args(git: GitWorkspace, tip: str, baseline_sha: str | None, candidate_sha: str) -> tuple[str, ...]:
    """Replay only the task's own commits (`baseline..candidate`) onto `tip`.

    A plain `git rebase <tip>` replays everything since the merge-base with the tip. When the candidate's recorded baseline carries
    commits that reached the tip by another route (a squash or merge of an earlier task's PR), that replays foreign commits that
    conflict with their own landed copies although the task's change itself applies cleanly.
    """
    if baseline_sha and baseline_sha != candidate_sha and git.run("merge-base", "--is-ancestor", baseline_sha, candidate_sha, check=False).returncode == 0:
        return ("rebase", "--onto", tip, baseline_sha)
    return ("rebase", tip)


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
        self.last_refresh_failure: str | None = None  # why the latest refresh() did not apply (names the conflicting files)

    def integrate(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> EvidenceStatus | None:  # type: ignore[override]
        with self.lock.hold(task_id):
            self._typed_failure = None
            if self.integration_ref is not None and self._reattribute(store, task_id, candidate_sha, project):
                return None  # sent back to VALIDATE with the same tree under the owner's identity
            if self.integration_ref is not None and self._diverged(project, candidate_sha) is not None and self._rebase(store, task_id, candidate_sha, project):
                return None  # sent back to VALIDATE with a rebased candidate
            try:
                return super().integrate(store, task_id, candidate_sha, project)
            finally:
                self._typed_failure = None

    def _resolve(self, project: Path) -> str | None:
        result = GitWorkspace(project).run("rev-parse", "--verify", "--quiet", f"{self.integration_ref}^{{commit}}", check=False)
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None

    def refresh_before_verification(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> bool:
        """Avoid verifying an already stale SHA; retain the normal refresh budget."""
        with self.lock.hold(task_id):
            self._typed_failure = None
            try:
                if self.integration_ref is None or self._diverged(project, candidate_sha) is None:
                    return False
                return self._rebase(store, task_id, candidate_sha, project)
            finally:
                self._typed_failure = None

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

    def _reattribute(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> bool:
        """Once per task: replace StageMesh-worker/provider authorship on a legacy candidate, keeping its tree (and so its content)."""
        if any(
            json.loads(row["payload"]).get("task_id") == task_id
            for row in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=?", (REATTRIBUTED_EVENT,))
        ):
            return False  # one attempt; if the offence persists the integration gate reports it
        try:
            bound = contract_for_candidate(store, task_id, candidate_sha, project)
            if not bound.baseline_sha or not _attribution_findings(project, bound.baseline_sha, candidate_sha):
                return False
            with owned_workspace(store, project, task_id, ExecutionKind.INTEGRATION) as lease:
                git = GitWorkspace(lease.path)
                git.run("reset", "--hard", "HEAD", check=False)
                git.run("clean", "-fdq", check=False)
                git.run("checkout", "--detach", candidate_sha)
                new_sha = reattribute_range(lease.path, bound.baseline_sha, candidate_sha)
                if new_sha is None:
                    git.run("checkout", "--detach", candidate_sha, check=False)
                    lease.seal(candidate_sha)
                    return False
                git.run("checkout", "--detach", new_sha)
                previous = store.latest_candidate(task_id)
                producer = str(previous["produced_by"]) if previous is not None else "reattribution"
                store.add_candidate(task_id, new_sha, producer, durable_handoff=True)
                lease.seal(new_sha)
                record_implementation_derivation(store, task_id, candidate_sha, new_sha, "reattribution")
        except (GitError, ContractError, OSError):
            return False
        store.bind_contract(task_id, new_sha, bound.baseline_sha, bound.version, bound.digest, bound.canonical_json)
        store.advance_task(task_id, Stage.VALIDATE)
        record_audit(store, REATTRIBUTED_EVENT, {"task_id": task_id, "from_candidate": candidate_sha, "candidate_sha": new_sha})
        self._emit(task_id, "integration_reattributed", {"from_candidate": candidate_sha, "candidate_sha": new_sha})
        return True

    def _rebase_count(self, store: Store, task_id: str) -> int:
        count = 0
        for row in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=?", (REBASED_EVENT,)):
            try:
                if json.loads(row["payload"]).get("task_id") == task_id:
                    count += 1
            except (TypeError, ValueError):
                continue
        return count

    def refresh(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> bool:
        """Recovery's refresh: rebase the candidate onto the current ref even though the automatic rebase budget is spent.

        True when a new candidate was produced and the task is back at VALIDATE (so it is validated and independently reviewed on the
        new SHA). False when there is nothing to refresh or the candidate does not apply; the ref is never touched either way.
        """
        with self.lock.hold(task_id):
            self._typed_failure = None
            self.last_refresh_failure = None
            try:
                if self.integration_ref is None or self._diverged(project, candidate_sha) is None:
                    return False
                applied = self._rebase(store, task_id, candidate_sha, project, budgeted=False)
                self.last_refresh_failure = None if applied or self._typed_failure is None else self._typed_failure[1]
                return applied
            finally:
                self._typed_failure = None

    def reconstruct_onto_current_tip(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> tuple[bool, str, list[str], str]:
        """Rebuild only the task-owned delta on the current integration tip.

        This is the bounded fallback for a task whose recorded baseline is not an ancestor of the integration tip. The baseline is
        intentionally not moved sideways: the original candidate and evidence remain intact, while a patch of `baseline..candidate`
        is applied to the current tip in the task worktree. The new SHA must then pass validation and exact-SHA review normally.
        """
        with self.lock.hold(task_id):
            tip = self._resolve(project)
            if tip is None:
                return False, "integration target is missing", [], "target_missing"
            try:
                bound = contract_for_candidate(store, task_id, candidate_sha, project)
                if not bound.baseline_sha:
                    return False, "candidate has no bound baseline, so the task-owned delta is ambiguous", [], "ambiguous_delta"
                root = GitWorkspace(project)
                baseline = bound.baseline_sha
                if root.run("merge-base", "--is-ancestor", baseline, candidate_sha, check=False).returncode != 0:
                    return False, "candidate does not descend from its bound baseline, so the task-owned delta is ambiguous", [], "ambiguous_delta"
                commits = [line for line in root.run("rev-list", "--reverse", f"{baseline}..{candidate_sha}").stdout.splitlines() if line]
                if not commits:
                    return False, "candidate has no task-owned commits to reconstruct", [], "ambiguous_delta"
                task_candidates = {
                    str(row["sha"]) for row in store.conn.execute("SELECT sha FROM candidates WHERE task_id=?", (task_id,))
                }
                foreign_commits = [commit for commit in commits if commit not in task_candidates]
                if foreign_commits:
                    return (
                        False,
                        "candidate history after the bound baseline contains commit(s) that are not recorded as candidates for this task",
                        foreign_commits[:10],
                        "ambiguous_delta",
                    )
                changed = changed_files(project, candidate_sha, baseline)
                if not changed:
                    return False, "candidate has no task-owned changed files to reconstruct", [], "ambiguous_delta"
                out_of_scope = [path for path in changed if not _matches(path, bound.contract.allowed_files)]
                if out_of_scope:
                    return False, "task-owned delta reaches files outside the contract", out_of_scope[:10], "out_of_scope_delta"
                # Git patches are byte protocols; text-mode stdin can change LF to CRLF on Windows.
                patch = subprocess.run(
                    ["git", "-c", "core.longpaths=true", "diff", "--binary", baseline, candidate_sha],
                    cwd=project, capture_output=True, check=False,
                )
                if patch.returncode != 0:
                    detail = (patch.stderr or patch.stdout).decode("utf-8", errors="replace").strip()[-1000:]
                    return False, detail or "could not read task-owned patch", [], "git_error"
                with owned_workspace(store, project, task_id, ExecutionKind.INTEGRATION) as lease:
                    worktree = lease.path
                    git = GitWorkspace(worktree)
                    try:
                        git.run("reset", "--hard", "HEAD", check=False)
                        git.run("clean", "-fdq", check=False)
                        git.run("checkout", "--detach", tip)
                        result = subprocess.run(
                            ["git", "-c", "core.longpaths=true", "apply", "--index", "--3way"],
                            cwd=worktree,
                            input=patch.stdout,
                            capture_output=True,
                            check=False,
                        )
                        if result.returncode != 0:
                            conflicts = [
                                line for line in git.run("diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines() if line
                            ]
                            git.run("reset", "--hard", tip, check=False)
                            lease.seal(tip)
                            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()[-1000:] or "patch did not apply"
                            return False, detail, conflicts[:10], "patch_conflict" if conflicts else "patch_failed"
                        reconstructed = git.commit_all(f"reconstruct {task_id} on current integration tip")
                    except (GitError, OSError) as exc:
                        git.run("reset", "--hard", tip, check=False)
                        lease.seal(tip)
                        return False, str(exc)[:300], [], "git_error"
                    if reconstructed == tip:
                        git.run("reset", "--hard", tip, check=False)
                        lease.seal(tip)
                        return False, "reconstruction produced no change on the current integration tip", changed[:10], "empty_reconstruction"
                    _preserve_original(project, task_id, candidate_sha)
                    previous = store.latest_candidate(task_id)
                    producer = str(previous["produced_by"]) if previous is not None else "reconstruction"
                    store.add_candidate(task_id, reconstructed, producer, durable_handoff=True)
                    lease.seal(reconstructed)
                    record_implementation_derivation(store, task_id, candidate_sha, reconstructed, "reconstruction")
            except (GitError, ContractError, OSError) as exc:
                return False, str(exc)[:300], [], "git_error"
            store.bind_contract(task_id, reconstructed, tip, bound.version, bound.digest, bound.canonical_json)
            store.advance_task(task_id, Stage.VALIDATE)
            record_audit(
                store,
                RECONSTRUCTED_EVENT,
                {
                    "task_id": task_id,
                    "from_candidate": candidate_sha,
                    "candidate_sha": reconstructed,
                    "old_baseline": bound.baseline_sha,
                    "onto": tip,
                    "ref": str(self.integration_ref),
                    "changed_files": changed,
                },
            )
            self._emit(task_id, "integration_reconstructed", {"from_candidate": candidate_sha, "candidate_sha": reconstructed, "onto": tip})
            return True, "", [], "applied"

    def _rebase(self, store: Store, task_id: str, candidate_sha: str, project: Path, *, budgeted: bool = True) -> bool:
        tip = self._resolve(project)
        if tip is None:
            return False
        attempts = self._rebase_count(store, task_id)
        if budgeted and attempts >= self.max_rebases:
            self._typed_failure = (
                STALE_BASE,
                (
                    f"{self.integration_ref} advanced to {tip} again after {attempts} automatic rebase(s) of task {task_id}; "
                    "ref left unchanged. Retry the task explicitly once the ref settles."
                ),
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
                result = git.run(*_rebase_args(git, tip, bound.baseline_sha, candidate_sha), check=False)
                if result.returncode != 0:
                    conflicts = [
                        line for line in git.run("diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines() if line
                    ]
                    git.run("rebase", "--abort", check=False)
                    git.run("checkout", "--detach", candidate_sha, check=False)
                    lease.seal(candidate_sha)
                    failure_code = REBASE_CONFLICT if conflicts else REBASE_UNAVAILABLE
                    self._typed_failure = (
                        failure_code,
                        f"{self.integration_ref} advanced to {tip}; candidate {candidate_sha}"
                        + (f" conflicts in {', '.join(conflicts[:10])}" if conflicts else f" could not be rebased: {(result.stderr or result.stdout).strip()[-1000:]}")
                        + "; ref left unchanged",
                    )
                    self._emit(task_id, failure_code, {"ref_tip": tip, "conflicts": conflicts[:10]})
                    return False
                rebased = git.head()
                _preserve_original(project, task_id, candidate_sha)
                previous = store.latest_candidate(task_id)
                producer = str(previous["produced_by"]) if previous is not None else "rebase"
                store.add_candidate(task_id, rebased, producer, durable_handoff=True)
                lease.seal(rebased)
                record_implementation_derivation(store, task_id, candidate_sha, rebased, "rebase")
        except (GitError, ContractError, OSError) as exc:
            self._typed_failure = (REBASE_UNAVAILABLE, f"could not rebase candidate {candidate_sha} onto {tip}: {exc}")
            self._emit(task_id, REBASE_UNAVAILABLE, {"ref_tip": tip, "error": str(exc)[:200]})
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
