"""The deterministic development supervisor.

`Supervisor` observes durable state and git, applies the pure policies in this package, performs only non-destructive actions
(create-only refs, replacement candidates, preserved snapshots) and records every decision in the audit-log trace. LLMs may implement,
review and help diagnose; none of the decisions made here consult one.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from pathlib import Path

from ..audit import record_audit
from ..baseline import resolve_integration_ref
from ..contract_binding import contract_for_candidate
from ..domain import EvidenceKind, EvidenceStatus, Stage
from ..git import GitError
from ..persistence import Store
from ..process_identity import classify_process, process_identity
from ..review import independent_review_verified
from ..serialized_integration import REBASED_EVENT
from ..workspaces import _WORKTREE_CREATION, _task_key, advance_worktree_generation, task_workspace
from . import base_state as base
from .ci_diagnosis import (
    CIClass,
    CIDiagnosis,
    HostedCI,
    TestObservation,
    diagnose_ci,
    observations_from_log,
    plan_ci_response,
)
from .decisions import (
    DECISION_EVENT,
    Action,
    AutonomyDecision,
    Condition,
    Escalation,
    EscalationReason,
    TaskOutcome,
    decision_trace,
    record_decision,
    record_task_outcome,
)
from .dependencies import (
    DependencyAssessment,
    PRDependency,
    PullRequestAdapter,
    evaluate_dependencies,
    load_pull_requests,
)
from .gitfacts import GitFacts
from .github_adapter import GitHubAuthorizationError, GitHubRateLimited, authorization_escalation
from .merge_policy import (
    IntegrationPolicy,
    MergeFacts,
    MergeVerdict,
    PostMergeCheck,
    PostMergeVerdict,
    verify_integration,
)
from .provenance import (
    CandidateProvenance,
    Mutation,
    WorkspaceOwnership,
    check_ownership,
    claim_workspace,
    load_ownership,
    load_provenance,
    record_lineage,
    revoke_evidence,
    save_ownership,
    tracked_fingerprint,
)
from .recovery_policy import (
    GitOperationRequest,
    RecoveryPolicy,
    decide_execution_recovery,
    decide_git_operation,
)
from .review_policy import ReviewAssessment, ReviewReport, assess_review
from .scope import DeferredItem, TaskScope, deferred_items, record_deferred

POLICY = "supervisor/v1"
DEPENDENCY_EVENT = "autonomy.pr_dependency"
INTEGRATED_EVENT = "autonomy.integrated"
_BLOCKING_SEVERITIES = frozenset({"blocker", "critical", "high", "major", "error"})


class ExternalWorkspaceMutation(RuntimeError):
    """Raised when an execution-owned worktree was changed by something other than its owner. Typed: never adopted."""

    code = "EXTERNAL_WORKSPACE_MUTATION"

    def __init__(self, decision: AutonomyDecision):
        self.decision = decision
        super().__init__(f"{self.code}: {decision.trace_line()}")


class Supervisor:
    def __init__(
        self,
        store: Store,
        project: Path,
        *,
        integration_ref: str | None = None,
        hosted_ci: HostedCI | None = None,
        pull_requests: PullRequestAdapter | None = None,
        integration_policy: IntegrationPolicy | None = None,
        recovery_policy: RecoveryPolicy | None = None,
        max_reconstructs: int = 1,
        max_remediations: int = 3,
        post_merge_checks: Sequence[Callable[[], PostMergeCheck]] = (),
    ):
        self.store = store
        self.project = Path(project).resolve()
        self.facts = GitFacts(self.project)
        self.integration_ref = integration_ref or resolve_integration_ref(self.project) or "HEAD"
        self.hosted_ci = hosted_ci
        self.pull_requests = pull_requests
        self.integration_policy = integration_policy or IntegrationPolicy()
        self.recovery_policy = recovery_policy or RecoveryPolicy()
        self.max_reconstructs = max_reconstructs
        self.max_remediations = max_remediations
        self.post_merge_checks = tuple(post_merge_checks)
        self.last_ci_diagnosis: CIDiagnosis | None = None  # structured companion of the last assess_ci decision

    # --- trace ----------------------------------------------------------------------------------------------------------------

    def record(self, decision: AutonomyDecision) -> AutonomyDecision:
        """Write the decision to the durable trace unless it is identical to the task's most recent one."""
        if decision.task_id is not None:
            last = self.store.conn.execute(
                "SELECT payload FROM audit_events WHERE event_type=? AND json_extract(payload, '$.task_id')=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (DECISION_EVENT, decision.task_id),
            ).fetchone()
            if last is not None and json.loads(last["payload"]).get("trace") == decision.trace_line():
                return decision
        record_decision(self.store, decision)
        return decision

    def trace(self, task_id: str | None = None) -> list[dict[str, object]]:
        return decision_trace(self.store, task_id)

    # --- capability 1: candidate integrity --------------------------------------------------------------------------------------

    def provenance(self, task_id: str) -> CandidateProvenance:
        return load_provenance(self.store, task_id)

    # --- capability 2: external workspace mutation --------------------------------------------------------------------------------

    def claim_workspace(self, task_id: str, worktree: Path, **kwargs: object) -> WorkspaceOwnership:
        return claim_workspace(self.store, task_id, Path(worktree), **kwargs)  # type: ignore[arg-type]

    def check_workspace(
        self, task_id: str, *, fetch: bool = False, execution_running: bool | None = None, restore: bool | None = None
    ) -> AutonomyDecision | None:
        """None when the workspace is as StageMesh left it; otherwise a recorded EXTERNAL_WORKSPACE_MUTATION decision.

        `execution_running` means the owner may legitimately be editing tracked files. `restore` (default: only when no execution is
        running) resets the owned worktree to what was recorded after the foreign state has been preserved under a quarantine ref.
        """
        ownership = load_ownership(self.store, task_id)
        if ownership is None:
            return None
        running = self._execution_running(task_id) if execution_running is None else execution_running
        candidate = self.store.latest_candidate(task_id)
        check = check_ownership(
            ownership,
            fetch=fetch,
            recorded_candidate=str(candidate["sha"]) if candidate is not None else None,
            execution_running=running,
        )
        if check.clean:
            if check.adopted_owner_advance:  # only commits by StageMesh/the owning provider were added
                save_ownership(
                    self.store,
                    replace(
                        ownership,
                        expected_head=check.adopted_owner_advance,
                        tracked_fingerprint=tracked_fingerprint(Path(ownership.worktree)),
                    ),
                )
            return None
        return self.record(self._quarantine(task_id, ownership, check.mutations, (not running) if restore is None else restore))

    def require_clean_workspace(self, task_id: str, *, fetch: bool = False) -> None:
        decision = self.check_workspace(task_id, fetch=fetch)
        if decision is not None:
            raise ExternalWorkspaceMutation(decision)

    def _quarantine(self, task_id: str, ownership: WorkspaceOwnership, mutations: list[Mutation], restore: bool) -> AutonomyDecision:
        """Preserve what the other writer left, restore the owned workspace to what was recorded, adopt nothing."""
        key = _task_key(task_id)
        worktree = Path(ownership.worktree)
        refs: dict[str, str] = {}
        restored = False
        replacement_branch: str | None = None
        local = [m for m in mutations if m.kind in {"HEAD_MOVED", "HEAD_REWRITTEN", "TRACKED_FILES_MODIFIED", "CANDIDATE_PROVENANCE_MISMATCH"}]
        for mutation in mutations:
            if mutation.kind in {"CANDIDATE_REF_MOVED", "REMOTE_REF_MOVED"} and self.facts.exists(mutation.observed):
                ref = f"refs/stagemesh/quarantine/{key}/{mutation.observed[:12]}"
                self.facts.ensure_ref(ref, mutation.observed)
                refs[mutation.kind] = ref
        if local and (worktree / ".git").exists():
            snapshot = GitFacts(worktree).snapshot_worktree(worktree, f"StageMesh quarantine of {task_id}: preserved external mutation")
            if snapshot:
                ref = f"refs/stagemesh/quarantine/{key}/{snapshot[:12]}"
                self.facts.ensure_ref(ref, snapshot)
                refs["worktree_snapshot"] = ref
            if restore:  # never reset a worktree an execution may still be writing to
                work = GitFacts(worktree)
                work.git.run("reset", "--hard", ownership.expected_head, check=False)
                work.git.run("clean", "-fdq", check=False)
                restored = work.resolve("HEAD") == ownership.expected_head
        for mutation in mutations:
            if mutation.kind == "CANDIDATE_REF_MOVED" and ownership.candidate_ref and ownership.expected_ref_tip and restore:
                moved = self.facts.git.run("update-ref", ownership.candidate_ref, ownership.expected_ref_tip, mutation.observed, check=False)
                restored = restored or moved.returncode == 0
        candidate = self.store.latest_candidate(task_id)
        if any(m.kind == "REMOTE_REF_MOVED" for m in mutations) and candidate is not None:
            branch = (ownership.remote_branch or "candidate").replace("/", "-")
            replacement_branch = f"refs/heads/{branch}-sm-{str(candidate['sha'])[:7]}"
            self.facts.ensure_ref(replacement_branch, str(candidate["sha"]))
            refs["replacement_branch"] = replacement_branch
        tainted = self._candidate_tainted(ownership)
        if tainted and candidate is not None:
            revoke_evidence(self.store, task_id, str(candidate["sha"]), "candidate contains commits by an untrusted committer")
        first = mutations[0]
        foreign = sorted({sha for m in mutations for sha in m.foreign_commits})
        head_now = GitFacts(worktree).resolve("HEAD") if (worktree / ".git").exists() else None
        return AutonomyDecision(
            Condition.EXTERNAL_WORKSPACE_MUTATION,
            POLICY,
            Action.FAIL_CLOSED_QUARANTINE,
            task_id,
            {"mutation": ",".join(sorted({m.kind for m in mutations})), "workspace": ownership.worktree},
            {
                "expected_head": ownership.expected_head,
                **({"observed_head": first.observed} if first.kind.startswith("HEAD") else {}),
                **({"foreign_commit": foreign[0]} if foreign else {}),
            },
            {
                "mutations": [m.to_dict() for m in mutations],
                "adopted": False,
                "quarantine_refs": refs,
                "workspace_restored_to_recorded_head": restored,
                "head_after": head_now,
                "candidate_tainted": tainted,
                "evidence_for_mutated_state_authorizes_integration": False,
                "published_replacement_required": replacement_branch is not None,
            },
        )

    def _candidate_tainted(self, ownership: WorkspaceOwnership) -> bool:
        """True when the latest candidate itself contains commits by an identity StageMesh does not trust."""
        candidate = self.store.latest_candidate(ownership.task_id)
        baseline = self.store.task_baseline(ownership.task_id)
        if candidate is None or baseline is None or not self.facts.exists(str(candidate["sha"])) or not self.facts.exists(baseline):
            return False
        if not self.facts.is_ancestor(baseline, str(candidate["sha"])):
            return False
        trusted = {e.casefold() for e in ownership.trusted_committer_emails}
        return any(
            self.facts.commit(sha).committer_email.casefold() not in trusted
            for sha in self.facts.commits_between(baseline, str(candidate["sha"]))
        )

    def _execution_running(self, task_id: str) -> bool:
        return any(row["task_id"] == task_id for row in self.store.running_executions())

    # --- capabilities 3 and 4: base advancement and history rewrite -----------------------------------------------------------------

    def reconcile_base(self, task_id: str, *, new_base_ref: str | None = None, dependency_landed: bool = False) -> AutonomyDecision | None:
        """Observe the integration base, classify it, and act: refresh, retarget, reconstruct or escalate.

        Never rewrites the original candidate. Replacement candidates are new SHAs with provenance; the task returns to VALIDATE so
        validation and independent review are rerun on the exact replacement.
        """
        candidate_row = self.store.latest_candidate(task_id)
        if candidate_row is None:
            return None
        candidate = str(candidate_row["sha"])
        binding = self.store.contract_binding(task_id, candidate)
        old_base = str(binding["baseline_sha"]) if binding is not None and binding["baseline_sha"] else self.store.task_baseline(task_id)
        new_base = self.facts.resolve(new_base_ref or self.integration_ref)
        state = base.classify_base(self.facts, candidate=candidate, old_base=old_base, new_base=new_base, dependency_landed=dependency_landed)
        left = max(0, self.max_reconstructs - self._reconstruct_count(task_id))
        plan = base.plan_base_response(state, task_id=task_id, reconstruct_attempts_left=left)
        if plan is None:
            return None
        if plan.action in {Action.PROCEED, Action.ESCALATE_TO_FOUNDER}:
            return self.record(plan)
        assert old_base is not None and new_base is not None
        key = _task_key(task_id)
        result = base.build_replacement_candidate(
            self.facts, task_key=key, candidate=candidate, old_base=old_base, new_base=new_base, reason=state.condition.value
        )
        proof_ok = result.ok and result.proof is not None and (result.proof.proven or not state.tree_equivalent)
        if not result.ok or not proof_ok:
            if result.ok and result.proof is not None:
                result.reason = "equivalence could not be proven for the transplanted candidate"
                result.conflicts = ("<equivalence unproven>",)
            decision = base.conflict_decision(state, result, task_id=task_id, reconstruct_attempts_left=left)
            if decision.action is Action.RECONSTRUCT_ON_NEW_BASE:
                self._apply_reconstruct(task_id, candidate, new_base, result)
            return self.record(decision)
        replacement = str(result.replacement)
        producer = str(candidate_row["produced_by"])
        bound = contract_for_candidate(self.store, task_id, candidate, self.project)
        self.store.add_candidate(task_id, replacement, producer, durable_handoff=True)
        self.store.bind_contract(task_id, replacement, new_base, bound.version, bound.digest, bound.canonical_json)
        self.store.advance_task(task_id, Stage.VALIDATE)
        record_lineage(
            self.store,
            task_id,
            candidate,
            replacement,
            state.condition.value,
            old_base=old_base,
            new_base=new_base,
            preserved_ref=result.preserved_ref,
            replacement_ref=result.replacement_ref,
        )
        record_audit(  # keeps the existing rebase budget (SerializedIntegrator) accurate
            self.store,
            REBASED_EVENT,
            {"task_id": task_id, "from_candidate": candidate, "candidate_sha": replacement, "onto": new_base, "ref": self.integration_ref},
        )
        self._rebind_ownership(task_id, replacement)
        rewritten = state.condition is not Condition.BASE_ADVANCED
        action = Action.CREATE_RETARGETED_CANDIDATE if rewritten else Action.REFRESH_CANDIDATE
        observed = state.observed()
        decision = AutonomyDecision(
            state.condition,
            base.POLICY,
            action,
            task_id,
            observed,
            {"original_candidate": candidate, "replacement_candidate": replacement},
            {
                "proof": result.proof.to_dict() if result.proof else None,
                "preserved_original_ref": result.preserved_ref,
                "replacement_ref": result.replacement_ref,
                "transplanted_commits": list(result.transplanted),
                "dropped_commits": list(result.dropped_commits),
                "evidence_invalidated": ["VALIDATION", "REVIEW"],
                "task_stage_after": "VALIDATE",
                "original_candidate_rewritten": False,
                "force_push": False,
            },
        )
        return self.record(decision)

    def _reconstruct_count(self, task_id: str) -> int:
        return sum(1 for d in decision_trace(self.store, task_id) if d.get("action") == Action.RECONSTRUCT_ON_NEW_BASE.value)

    def _apply_reconstruct(self, task_id: str, candidate: str, new_base: str, result: base.RetargetResult) -> None:
        """Fresh implementation attempt on the new base; the conflicting original is preserved and described to the agent."""
        self.facts.ensure_ref(f"refs/stagemesh/preserved/{_task_key(task_id)}/{candidate[:12]}", candidate)
        message = (
            f"candidate {candidate[:7]} conflicts with base {new_base[:7]}"
            + (f" in {', '.join(result.conflicts[:5])}" if result.conflicts else "")
            + "; re-implement the objective on the current base"
        )
        from ..remediation import finding_identity

        self.store.upsert_finding(finding_identity(candidate, message), task_id, candidate, "error", message)
        self.store.add_task_remediation(task_id, "INTEGRATE", candidate)
        self.store.rebaseline_task(task_id, new_base, None)
        self.store.advance_task(task_id, Stage.IMPLEMENT)

    def _rebind_ownership(self, task_id: str, replacement: str) -> None:
        ownership = load_ownership(self.store, task_id)
        if ownership is not None:
            save_ownership(self.store, replace(ownership, expected_head=replacement))

    # --- capability 5: PR dependencies ---------------------------------------------------------------------------------------------

    def declare_dependency(self, task_id: str, pr: int, depends_on: int) -> None:
        record_audit(self.store, DEPENDENCY_EVENT, {"task_id": task_id, "pr": pr, "depends_on": depends_on})

    def declared_dependencies(self) -> list[PRDependency]:
        seen: dict[tuple[int, int], PRDependency] = {}
        for row in self.store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (DEPENDENCY_EVENT,)):
            payload = json.loads(row["payload"])
            seen[(int(payload["pr"]), int(payload["depends_on"]))] = PRDependency(int(payload["pr"]), int(payload["depends_on"]))
        return list(seen.values())

    def assess_dependencies(self, task_id: str, pr_number: int, *, apply: bool = True) -> DependencyAssessment:
        """Evaluate the PR's dependencies. When all have landed, retarget its base and refresh the candidate: it resumes by itself."""
        if self.pull_requests is None:
            raise RuntimeError("a PullRequestAdapter is required to assess PR dependencies")
        edges = self.declared_dependencies()
        numbers = {pr_number, *(e.depends_on for e in edges), *(e.pr for e in edges)}
        prs = load_pull_requests(self.pull_requests, sorted(numbers))
        previous = [d for d in decision_trace(self.store, task_id) if d.get("condition", "").startswith("DEPENDENCY_") or d.get("action") == Action.BLOCK_ON_DEPENDENCY.value]
        was_blocked = bool(previous) and previous[-1].get("action") == Action.BLOCK_ON_DEPENDENCY.value
        assessment = evaluate_dependencies(
            pr_number, prs, edges, integration_ref=self.integration_ref.removeprefix("refs/heads/"), was_blocked=was_blocked, task_id=task_id
        )
        self.record(assessment.decision)
        if apply and assessment.refresh_required:  # landed dependencies: refresh even if this PR was never seen blocked
            if assessment.retarget_base_to:
                self.pull_requests.set_base(pr_number, assessment.retarget_base_to)
            self.reconcile_base(task_id, dependency_landed=True)
        return assessment

    # --- capabilities 6 and 7: CI ----------------------------------------------------------------------------------------------------

    def assess_ci(
        self,
        task_id: str,
        candidate_sha: str,
        base_sha: str,
        *,
        dependency_sha: str | None = None,
        scope: TaskScope | None = None,
        observations: Iterable[TestObservation] = (),
        reruns_left: int = 1,
    ) -> AutonomyDecision:
        if self.hosted_ci is None:
            raise RuntimeError("a HostedCI adapter is required to diagnose CI")
        candidate_run = self.hosted_ci.run_for(candidate_sha)
        base_run = self.hosted_ci.run_for(base_sha)  # compared before anything is concluded about the candidate
        dependency_run = self.hosted_ci.run_for(dependency_sha) if dependency_sha else None
        found = list(observations)
        if candidate_run is not None:
            for gate in candidate_run.gates.values():
                found.extend(observations_from_log(gate.log))
        diagnosis = diagnose_ci(candidate_run, base_run, dependency=dependency_run, candidate_sha=candidate_sha, observations=found)
        decision = plan_ci_response(diagnosis, task_id=task_id, scope=scope, reruns_left=reruns_left)
        for gate in diagnosis.gates:
            if gate.klass is CIClass.BASELINE_FAILURE:  # recorded as deferred work; never "fixed" as part of this task
                record_deferred(self.store, task_id, DeferredItem(f"gate {gate.gate} already fails on base {base_sha[:7]}", "ci", None, candidate_sha))
        self.last_ci_diagnosis = diagnosis
        return self.record(decision)

    # --- capability 8: review -------------------------------------------------------------------------------------------------------

    def assess_review(self, task_id: str, report: ReviewReport, scope: TaskScope, *, candidate_sha: str | None = None, independent_required: bool = True) -> ReviewAssessment:
        sha = candidate_sha or str(self.store.latest_candidate(task_id)["sha"])
        assessment = assess_review(report, candidate_sha=sha, scope=scope, task_id=task_id, independent_required=independent_required)
        for item in assessment.deferred:
            record_deferred(self.store, task_id, item)
        self.record(assessment.decision)
        return assessment

    # --- capability 10: merge policy --------------------------------------------------------------------------------------------------

    def evaluate_merge(
        self,
        task_id: str,
        *,
        ci: CIDiagnosis | None,
        dependencies: DependencyAssessment | None = None,
        mergeable: bool | None = None,
        fetch: bool = False,
    ) -> MergeVerdict:
        prov = self.provenance(task_id)
        ownership = load_ownership(self.store, task_id)
        mutations: list[Mutation] = []
        if ownership is not None:
            mutations = check_ownership(
                ownership, fetch=fetch, recorded_candidate=prov.candidate_sha, execution_running=self._execution_running(task_id)
            ).mutations
        state = None
        if prov.candidate_sha:
            state = base.classify_base(
                self.facts,
                candidate=prov.candidate_sha,
                old_base=prov.baseline_sha,
                new_base=self.facts.resolve(self.integration_ref),
            )
        open_findings = [
            row for row in self.store.open_findings_for_candidate(task_id, prov.candidate_sha or "")
            if str(row["severity"]).casefold() in _BLOCKING_SEVERITIES
        ] if prov.candidate_sha else []
        facts = MergeFacts(
            provenance=prov,
            mutations=mutations,
            base=state,
            dependencies=dependencies,
            unresolved_blocking_findings=len(open_findings),
            review_independent=self._review_independent(task_id, prov.candidate_sha),
            ci=ci,
            mergeable=mergeable,
        )
        verdict = self.integration_policy.evaluate(facts, task_id=task_id)
        self.record(verdict.decision)
        return verdict

    def _review_independent(self, task_id: str, candidate_sha: str | None) -> bool:
        if not candidate_sha:
            return False
        rows = self.store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
            (task_id, candidate_sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED),
        )
        for row in rows:
            try:
                if independent_review_verified(json.loads(row["payload"])):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def verify_integration(self, task_id: str, candidate_sha: str, *, merge_sha: str | None = None) -> PostMergeVerdict:
        """After integration: record the resulting main SHA, verify landed content and run post-merge checks."""
        prov = self.provenance(task_id)
        verdict = verify_integration(
            self.facts,
            integration_ref=self.integration_ref,
            candidate_sha=candidate_sha,
            baseline_sha=prov.baseline_sha,
            merge_sha=merge_sha,
            post_merge_checks=self.post_merge_checks,
            task_id=task_id,
        )
        assert verdict.decision is not None
        if verdict.verified:
            record_audit(
                self.store,
                INTEGRATED_EVENT,
                {"task_id": task_id, "candidate_sha": candidate_sha, "integration_sha": verdict.integration_sha, "ref": self.integration_ref},
            )
        self.record(verdict.decision)
        return verdict

    def merge_when_ready(
        self,
        task_id: str,
        pr_number: int,
        *,
        ci: CIDiagnosis | None = None,
        method: str = "squash",
        remote: str | None = None,
    ) -> AutonomyDecision:
        """Merge the PR only when `IntegrationPolicy` is satisfied, pinned to the exact validated head, then verify before DONE."""
        if self.pull_requests is None:
            raise RuntimeError("a PullRequestAdapter is required to merge")
        try:
            return self._merge_when_ready(task_id, pr_number, ci, method, remote)
        except GitHubAuthorizationError as error:
            return self.record(
                AutonomyDecision(
                    Condition.MERGE_POLICY_UNSATISFIED,
                    POLICY,
                    Action.ESCALATE_TO_FOUNDER,
                    task_id,
                    {"pr": str(pr_number), "github_status": str(error.status)},
                    {},
                    {},
                    authorization_escalation(error, f"permission to read and merge pull request #{pr_number}"),
                )
            )
        except GitHubRateLimited as error:
            return self.record(
                AutonomyDecision(
                    Condition.MERGE_POLICY_UNSATISFIED,
                    POLICY,
                    Action.WAIT,
                    task_id,
                    {"pr": str(pr_number), "reason": "github_rate_limited"},
                    {},
                    {"retry_after_seconds": error.retry_after},
                )
            )

    def _merge_when_ready(self, task_id: str, pr_number: int, ci: CIDiagnosis | None, method: str, remote: str | None) -> AutonomyDecision:
        assert self.pull_requests is not None
        prov = self.provenance(task_id)
        pr = self.pull_requests.get(pr_number)
        if pr is None or prov.candidate_sha is None:
            return self.record(
                AutonomyDecision(Condition.MERGE_POLICY_UNSATISFIED, POLICY, Action.WAIT, task_id, {"pr": str(pr_number), "reason": "pull request or candidate unknown"})
            )
        dependencies = None
        if any(edge.pr == pr_number for edge in self.declared_dependencies()):
            dependencies = self.assess_dependencies(task_id, pr_number, apply=False)
        verdict = self.evaluate_merge(task_id, ci=ci, dependencies=dependencies, mergeable=pr.mergeable)
        if not verdict.may_merge:
            return verdict.decision
        if pr.head_sha != prov.candidate_sha:
            return self.record(self._pr_head_moved(task_id, pr_number, prov.candidate_sha, pr.head_sha))
        result = self.pull_requests.merge(pr_number, prov.candidate_sha, method)
        if not result.merged:
            if result.reason == "head_sha_mismatch":
                return self.record(self._pr_head_moved(task_id, pr_number, prov.candidate_sha, "unknown-after-validation"))
            return self.record(
                AutonomyDecision(
                    Condition.MERGE_POLICY_UNSATISFIED, POLICY, Action.WAIT, task_id, {"pr": str(pr_number), "merge_refused": result.reason[:120]}, {"candidate": prov.candidate_sha}
                )
            )
        if remote:
            self.facts.git.run("fetch", "--quiet", remote, self.integration_ref.removeprefix("refs/heads/"), check=False)
        post = self.verify_integration(task_id, prov.candidate_sha, merge_sha=result.sha)
        if not post.verified:
            return post.decision  # type: ignore[return-value]
        bound = contract_for_candidate(self.store, task_id, prov.candidate_sha, self.project)
        self.store.add_evidence(
            task_id,
            prov.candidate_sha,
            EvidenceKind.INTEGRATION,
            EvidenceStatus.PASSED,
            {
                **bound.evidence_payload(),
                "integrator": "pull_request_merge",
                "pull_request": pr_number,
                "merge_method": method,
                "integration_ref": self.integration_ref,
                "integration_ref_after": post.integration_sha,
                "merge_commit_sha": result.sha,
            },
        )
        self.store.advance_task(task_id, Stage.DONE)
        return self.record(
            AutonomyDecision(
                Condition.MERGE_POLICY_SATISFIED,
                POLICY,
                Action.MERGE,
                task_id,
                {"pr": str(pr_number), "method": method},
                {"candidate": prov.candidate_sha, "merge": result.sha or "none", "integration": post.integration_sha or "unknown"},
                {"verified": True, "task_stage": "DONE", "head_pinned": True},
            )
        )

    def _pr_head_moved(self, task_id: str, pr_number: int, validated: str, observed: str) -> AutonomyDecision:
        """The PR branch no longer points at the validated candidate: someone else wrote to it. Nothing is merged or adopted."""
        key = _task_key(task_id)
        replacement = f"refs/heads/pr-{pr_number}-sm-{validated[:7]}"
        if self.facts.exists(validated):
            self.facts.ensure_ref(replacement, validated)
        quarantine = None
        if self.facts.exists(observed):
            quarantine = f"refs/stagemesh/quarantine/{key}/{observed[:12]}"
            self.facts.ensure_ref(quarantine, observed)
        return AutonomyDecision(
            Condition.EXTERNAL_WORKSPACE_MUTATION,
            POLICY,
            Action.FAIL_CLOSED_QUARANTINE,
            task_id,
            {"mutation": "PR_HEAD_MOVED", "pr": str(pr_number)},
            {"validated_head": validated, "observed_head": observed},
            {
                "adopted": False,
                "merged": False,
                "quarantine_ref": quarantine,
                "replacement_branch": replacement,
                "evidence_for_observed_head": "none: validation and review are bound to the validated head only",
            },
        )

    def finish_task(self, task_id: str, *, operational_interventions: int = 0, notes: str = "") -> bool:
        """After a verified DONE, record the outcome for the ten-task Founder Hands-Off streak. Refuses to record an unverified DONE."""
        task = self.store.get_task(task_id)
        prov = self.provenance(task_id)
        if task is None or task["stage"] != Stage.DONE or prov.integration_sha is None:
            return False
        escalations = tuple(
            str(d["escalation"]["reason"]) for d in decision_trace(self.store, task_id) if d.get("escalation")
        )
        record_task_outcome(self.store, TaskOutcome(task_id, True, operational_interventions, escalations, notes))
        return True

    # --- recovery policy (K) and destructive operations (L) -----------------------------------------------------------------------------

    def _assess_execution(self, task_id: str, execution_id: str, age_seconds: float) -> AutonomyDecision:
        saved = self.store.execution_process_identity(execution_id)
        state = classify_process(saved, process_identity(saved.pid))
        return decide_execution_recovery(task_id, execution_id, state, age_seconds=age_seconds, policy=self.recovery_policy)

    def assess_execution(self, task_id: str, execution_id: str, *, age_seconds: float = 0.0) -> AutonomyDecision:
        """Decide only (no side effects): LIVE waits, DEAD may be released, UNKNOWN is held or fenced, never released."""
        return self.record(self._assess_execution(task_id, execution_id, age_seconds))

    def recover_execution(self, task_id: str, execution_id: str, *, age_seconds: float = 0.0) -> AutonomyDecision:
        """Decide and act. A provably dead execution is released; an unknown one is fenced onto a replacement worktree."""
        decision = self._assess_execution(task_id, execution_id, age_seconds)
        if decision.action is Action.RELEASE_DEAD_EXECUTION:
            if not self.store.recover_stale_execution_claim(execution_id, "DEAD_PROCESS_IDENTITY"):
                self.store.mark_orphan_running_execution_failed(execution_id, "DEAD_PROCESS_IDENTITY")
        elif decision.action is Action.FENCE_AND_REPLACE_EXECUTION:
            decision = replace(decision, detail={**decision.detail, **self._fence(task_id, execution_id)})
        return self.record(decision)

    def recover_unknown(self, task_id: str, execution_id: str) -> bool:
        """Coordinator hook for an UNKNOWN-identity execution: True when it was fenced and the task may proceed, False to keep waiting."""
        row = self.store.conn.execute("SELECT started_at FROM executions WHERE id=?", (execution_id,)).fetchone()
        age = max(0.0, time.time() - float(row["started_at"])) if row is not None else 0.0
        decision = self.recover_execution(task_id, execution_id, age_seconds=age)
        return decision.action is Action.FENCE_AND_REPLACE_EXECUTION

    def _fence(self, task_id: str, execution_id: str) -> dict[str, object]:
        """Preserve the old worktree untouched, release the claim without declaring death, continue on a new worktree generation."""
        key = _task_key(task_id)
        old_path = task_workspace(self.project, task_id)
        preserved: str | None = None
        if (old_path / ".git").exists():
            snapshot = GitFacts(old_path).snapshot_worktree(old_path, f"StageMesh fence of {task_id}: execution {execution_id} identity unknown")
            if snapshot:
                preserved = f"refs/stagemesh/quarantine/{key}/{snapshot[:12]}"
                self.facts.ensure_ref(preserved, snapshot)
        candidate = self.store.latest_candidate(task_id)
        start = str(candidate["sha"]) if candidate is not None else self.store.task_baseline(task_id) or self.facts.resolve("HEAD")
        if start is None:
            raise GitError(f"cannot fence {task_id}: no commit to start the replacement worktree from")
        self.store.fence_unknown_execution(execution_id, "process identity UNKNOWN; replaced under the explicit recovery policy")
        old, new = advance_worktree_generation(self.project, task_id)
        with _WORKTREE_CREATION:
            self.facts.git.run("worktree", "add", "--detach", str(new), start)
            GitFacts(new).git.run("config", "user.email", "stagemesh@example.invalid")
            GitFacts(new).git.run("config", "user.name", "StageMesh")
        self.claim_workspace(task_id, new)
        return {
            "fenced_execution_status": "UNKNOWN",
            "old_worktree": str(old),
            "new_worktree": str(new),
            "replacement_starts_at": start,
            "preserved_old_state_ref": preserved,
        }

    def assess_git_operation(self, task_id: str, request: GitOperationRequest) -> AutonomyDecision:
        decision = decide_git_operation(request, task_id=task_id)
        if decision.action in {Action.PRESERVE_THEN_PROCEED, Action.USE_REPLACEMENT_BRANCH} and request.target_sha and self.facts.exists(request.target_sha):
            ref = f"refs/stagemesh/preserved/{_task_key(task_id)}/{request.target_sha[:12]}"
            self.facts.ensure_ref(ref, request.target_sha)
            decision = replace(decision, detail={**decision.detail, "preserved_ref": ref})
        return self.record(decision)

    # --- coordinator guard ----------------------------------------------------------------------------------------------------------------

    def allow(self, stage: Stage, task_id: str, sha: str) -> bool:
        """Coordinator hook: False keeps the task where it is. Never raises, never adopts."""
        try:
            if self.check_workspace(task_id) is not None:
                return False
            if stage is Stage.INTEGRATE and self.hosted_ci is not None and not self._ci_allows(task_id, sha):
                return False
            if stage is Stage.INTEGRATE:
                problems = self.provenance(task_id).evidence_problems()
                if problems:
                    self.record(
                        AutonomyDecision(
                            Condition.EVIDENCE_NOT_BOUND_TO_CANDIDATE,
                            POLICY,
                            Action.REVOKE_EVIDENCE_REQUIRE_REVALIDATION,
                            task_id,
                            {"problems": "; ".join(problems)[:300]},
                            {"candidate": sha},
                        )
                    )
                    return False
            return True
        except GitError:
            return False  # cannot observe the workspace: fail closed

    def _ci_allows(self, task_id: str, sha: str) -> bool:
        """Hosted-CI gate before integration: diagnose against base CI; remediate only what the candidate itself broke."""
        base_sha = self.facts.resolve(self.integration_ref)
        if base_sha is None:
            return False
        decision = self.assess_ci(task_id, sha, base_sha, scope=self._scope(task_id, sha))
        diagnosis = self.last_ci_diagnosis
        assert diagnosis is not None
        if not diagnosis.merge_blockers(allow_baseline_failures=self.integration_policy.allow_baseline_ci_failures):
            return True
        if decision.action in {Action.REMEDIATE_CANDIDATE, Action.FIX_TEST_FIXTURE}:
            self._send_back_for_ci_remediation(task_id, sha, diagnosis, decision)
        return False

    def _scope(self, task_id: str, sha: str) -> TaskScope | None:
        try:
            return TaskScope.from_contract(contract_for_candidate(self.store, task_id, sha, self.project).contract, deferred_items(self.store, task_id))
        except Exception:  # noqa: BLE001 - scope only refines the response; a missing contract must not hide a CI failure
            return None

    def _send_back_for_ci_remediation(self, task_id: str, sha: str, diagnosis: CIDiagnosis, decision: AutonomyDecision) -> None:
        """The implementation agent is told exactly which gates to fix and which already-red gates it must leave alone."""
        from ..remediation import finding_identity

        fix = [g for g in diagnosis.gates if g.klass in {CIClass.CANDIDATE_REGRESSION, CIClass.BROKEN_FRAGILE_TEST}]
        leave = [g.gate for g in diagnosis.gates if g.klass is CIClass.BASELINE_FAILURE]
        used = self.store.task_remediation_count(task_id, "INTEGRATE")
        if used >= self.max_remediations:
            self.store.block_task(task_id)
            gates = ", ".join(g.gate for g in fix)
            self.record(
                AutonomyDecision(
                    Condition.REMEDIATION_BUDGET_EXHAUSTED,
                    POLICY,
                    Action.ESCALATE_TO_FOUNDER,
                    task_id,
                    {"remediation_attempts": str(used), "gates": gates},
                    {"candidate": sha},
                    {},
                    Escalation(
                        EscalationReason.REMEDIATION_BUDGET_EXHAUSTED,
                        attempted=(
                            f"{used} remediation attempts, each given the CI diagnosis and told which gates to leave alone",
                            "compared every candidate's CI with base CI to rule out baseline and infrastructure failures",
                        ),
                        why_undeterminable=f"gate(s) {gates} keep failing on candidates built to fix them, so either the objective cannot be met as specified or the gate is wrong",
                        smallest_decision=f"Is gate(s) {gates} a hard requirement of this objective, or should it be excluded from the acceptance criteria?",
                    ),
                )
            )
            return
        for gate in fix:
            message = f"hosted CI gate '{gate.gate}': {gate.reason}"
            if gate.new_failing_tests:
                message += f" (new failing tests: {', '.join(gate.new_failing_tests)})"
            if gate.test_defect is not None:
                message = f"hosted CI gate '{gate.gate}': {gate.test_defect.explanation}; do not change production behavior"
            if leave:
                message += f". Do not touch gates that already fail on base: {', '.join(leave)}"
            self.store.upsert_finding(finding_identity(sha, message), task_id, sha, "error", message)
        self.store.add_task_remediation(task_id, "INTEGRATE", sha)
        self.store.advance_task(task_id, Stage.IMPLEMENT)
        record_audit(self.store, "autonomy.ci_remediation_queued", {"task_id": task_id, "candidate_sha": sha, "gates": [g.gate for g in fix], "action": decision.action.value})

    def integration_verified(self, task_id: str, sha: str) -> bool:
        return self.verify_integration(task_id, sha).may_mark_done
