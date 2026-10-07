"""Merge policy: StageMesh merges only when every condition holds, and a task is DONE only after integration is verified.

`IntegrationPolicy.evaluate` checks, in a fixed precedence order: unexpected workspace mutations, exact-candidate evidence
binding, candidate freshness, base/dependency state, unresolved blocking findings, independent review, CI status and
mergeability. The first unsatisfied check decides the action; all of them are reported. `verify_integration` then proves the
expected content actually landed on the integration ref and runs the required post-merge checks before the task may be DONE.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .base_state import BaseState
from .ci_diagnosis import CIDiagnosis, condition_for, plan_ci_response
from .decisions import Action, AutonomyDecision, Condition, Escalation, EscalationReason
from .dependencies import DependencyAssessment
from .gitfacts import GitFacts
from .provenance import CandidateProvenance, Mutation

POLICY = "merge/v1"


@dataclass(frozen=True)
class MergeFacts:
    provenance: CandidateProvenance
    mutations: Sequence[Mutation] = ()
    base: BaseState | None = None  # None: freshness unknown, which is not fresh
    dependencies: DependencyAssessment | None = None
    unresolved_blocking_findings: int = 0
    review_independent: bool = False
    ci: CIDiagnosis | None = None
    mergeable: bool | None = None


@dataclass(frozen=True)
class PolicyCheck:
    name: str
    satisfied: bool
    reason: str
    condition: Condition = Condition.MERGE_POLICY_UNSATISFIED
    action: Action = Action.WAIT

    def to_dict(self) -> dict[str, object]:
        return {"check": self.name, "satisfied": self.satisfied, "reason": self.reason}


@dataclass
class MergeVerdict:
    checks: list[PolicyCheck]
    decision: AutonomyDecision

    @property
    def may_merge(self) -> bool:
        return self.decision.action is Action.MERGE

    @property
    def unsatisfied(self) -> list[PolicyCheck]:
        return [c for c in self.checks if not c.satisfied]


@dataclass(frozen=True)
class IntegrationPolicy:
    require_independent_review: bool = True
    allow_baseline_ci_failures: bool = True  # gates failing identically on base do not block, but are recorded
    baseline_requires_detail: bool = True  # gates classified baseline from conclusions alone (no logs/tests) do not make a candidate merge-ready
    require_mergeable: bool = True

    def evaluate(self, facts: MergeFacts, *, task_id: str | None = None) -> MergeVerdict:
        prov = facts.provenance
        checks: list[PolicyCheck] = []

        checks.append(
            PolicyCheck(
                "no_unexpected_workspace_mutation",
                not facts.mutations,
                "no external mutation observed" if not facts.mutations else "; ".join(f"{m.kind} {m.detail}".strip() for m in facts.mutations),
                Condition.EXTERNAL_WORKSPACE_MUTATION,
                Action.FAIL_CLOSED_QUARANTINE,
            )
        )
        problems = prov.evidence_problems(require_review=True)
        checks.append(
            PolicyCheck(
                "exact_candidate_evidence",
                not problems,
                "validation and review are bound to the candidate" if not problems else "; ".join(problems),
                Condition.EVIDENCE_NOT_BOUND_TO_CANDIDATE,
                Action.REVOKE_EVIDENCE_REQUIRE_REVALIDATION,
            )
        )
        fresh = facts.base is not None and not facts.base.candidate_stale and facts.base.condition in {
            Condition.BASE_UNCHANGED,
            Condition.BASE_ADVANCED,
        }
        checks.append(
            PolicyCheck(
                "candidate_fresh",
                fresh,
                "candidate contains the current base tip"
                if fresh
                else ("base state unknown" if facts.base is None else f"{facts.base.condition.value}: candidate must be refreshed"),
                facts.base.condition if facts.base is not None and not fresh else Condition.BASE_ADVANCED,
                Action.CREATE_RETARGETED_CANDIDATE
                if facts.base is not None and facts.base.condition in {Condition.BASE_HISTORY_REWRITTEN, Condition.BASE_HISTORY_REWRITTEN_CONTENT_CHANGED, Condition.BASE_DEPENDENCY_LANDED}
                else Action.REFRESH_CANDIDATE,
            )
        )
        deps_ok = facts.dependencies is None or not facts.dependencies.blocked
        checks.append(
            PolicyCheck(
                "dependencies_landed",
                deps_ok,
                "no unlanded dependencies" if deps_ok else f"waiting on {', '.join(f'#{n}' for n in facts.dependencies.blocked_on)}",  # type: ignore[union-attr]
                Condition.DEPENDENCY_RED if facts.dependencies is not None and facts.dependencies.red else Condition.DEPENDENCY_PENDING,
                Action.BLOCK_ON_DEPENDENCY,
            )
        )
        checks.append(
            PolicyCheck(
                "no_unresolved_blocking_findings",
                facts.unresolved_blocking_findings == 0,
                f"{facts.unresolved_blocking_findings} blocking finding(s) unresolved" if facts.unresolved_blocking_findings else "none",
                Condition.REVIEW_BLOCKING_IN_SCOPE,
                Action.REMEDIATE_CANDIDATE,
            )
        )
        independent_ok = facts.review_independent or not self.require_independent_review
        checks.append(
            PolicyCheck(
                "independent_review",
                independent_ok,
                "independent reviewer verified" if independent_ok else "review was not performed by a verified independent provider",
                Condition.REVIEW_NOT_INDEPENDENT,
                Action.REQUIRE_INDEPENDENT_REVIEW,
            )
        )
        if facts.ci is None:
            ci_check = PolicyCheck("ci", False, "no hosted CI result for the candidate", Condition.CI_PENDING, Action.WAIT)
        elif facts.ci.candidate_sha != prov.candidate_sha:
            ci_check = PolicyCheck(
                "ci", False, f"the CI diagnosis is for another candidate ({facts.ci.candidate_sha[:7]})", Condition.CI_PENDING, Action.WAIT
            )
        elif not facts.ci.gates:
            ci_check = PolicyCheck("ci", False, "no CI gates were observed for the candidate", Condition.CI_PENDING, Action.WAIT)
        else:
            blockers = facts.ci.merge_blockers(
                allow_baseline_failures=self.allow_baseline_ci_failures, baseline_requires_detail=self.baseline_requires_detail
            )
            ci_check = PolicyCheck(
                "ci",
                not blockers,
                "CI green (or only failures already present on base)" if not blockers else "; ".join(blockers),
                _ci_condition(facts.ci),
                _ci_action(facts.ci),
            )
        checks.append(ci_check)
        mergeable_ok = facts.mergeable is True or (facts.mergeable is None and not self.require_mergeable)
        checks.append(
            PolicyCheck(
                "mergeable",
                mergeable_ok,
                "host reports the PR mergeable" if mergeable_ok else ("mergeability unknown" if facts.mergeable is None else "host reports conflicts"),
                Condition.MERGE_POLICY_UNSATISFIED,
                Action.WAIT if facts.mergeable is None else Action.REFRESH_CANDIDATE,
            )
        )

        shas = {"candidate": prov.candidate_sha or "none", "validation": prov.validation_sha or "none", "review": prov.review_sha or "none"}
        failing = [c for c in checks if not c.satisfied]
        detail = {"checks": [c.to_dict() for c in checks], "baseline_ci_failures": _baseline_gates(facts.ci)}
        if not failing:
            decision = AutonomyDecision(Condition.MERGE_POLICY_SATISFIED, POLICY, Action.MERGE, task_id, {"checks": str(len(checks))}, shas, detail)
        else:
            first = failing[0]
            decision = AutonomyDecision(
                first.condition,
                POLICY,
                first.action,
                task_id,
                {"unsatisfied": ",".join(c.name for c in failing)},
                shas,
                detail,
            )
        return MergeVerdict(checks, decision)


def _baseline_gates(ci: CIDiagnosis | None) -> list[str]:
    if ci is None:
        return []
    return [g.gate for g in ci.gates if g.klass.value == "BASELINE_FAILURE"]


def _ci_condition(ci: CIDiagnosis) -> Condition:
    return condition_for(ci.overall)


def _ci_action(ci: CIDiagnosis) -> Action:
    action = plan_ci_response(ci, task_id=None).action
    if action is Action.RECORD_BASELINE_FAILURE_AND_PROCEED:
        return Action.REQUEST_BASE_CI  # reaching here means the baseline evidence was too weak to merge on: get log-level evidence
    return action


# --- post-merge verification --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PostMergeCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class PostMergeVerdict:
    verified: bool
    integration_ref: str
    integration_sha: str | None
    missing_content: list[str] = field(default_factory=list)
    checks: list[PostMergeCheck] = field(default_factory=list)
    decision: AutonomyDecision | None = None

    @property
    def may_mark_done(self) -> bool:
        return self.verified


def verify_integration(
    facts: GitFacts,
    *,
    integration_ref: str,
    candidate_sha: str,
    baseline_sha: str | None,
    merge_sha: str | None = None,
    post_merge_checks: Sequence[Callable[[], PostMergeCheck]] = (),
    task_id: str | None = None,
) -> PostMergeVerdict:
    """Record the resulting main SHA, verify the candidate's content is on it, run post-merge checks. DONE only if all pass."""
    main_sha = facts.resolve(integration_ref)
    shas = {"candidate": candidate_sha, "integration": main_sha or "unresolved"}
    if merge_sha:
        shas["merge"] = merge_sha
    if main_sha is None:
        verdict = PostMergeVerdict(False, integration_ref, None)
        verdict.decision = AutonomyDecision(
            Condition.POST_MERGE_VERIFICATION_FAILED, POLICY, Action.WAIT, task_id, {"reason": "integration ref does not resolve"}, shas
        )
        return verdict
    if merge_sha and not facts.exists(merge_sha):
        verdict = PostMergeVerdict(False, integration_ref, main_sha)
        verdict.decision = AutonomyDecision(
            Condition.POST_MERGE_VERIFICATION_FAILED,
            POLICY,
            Action.WAIT,
            task_id,
            {"reason": "the landed commit is not observable locally yet"},
            shas,
            {"task_not_done": True, "retry": "verify again once the integration ref has been fetched"},
        )
        return verdict  # not evidence of anything wrong with the content: nobody is asked whether to revert
    landed_at = merge_sha or main_sha
    reachable = landed_at == main_sha or facts.is_ancestor(landed_at, main_sha)
    missing: list[str] = []
    if baseline_sha and facts.exists(baseline_sha):
        expected_tree: str | None = None
        for path in facts.changed_paths(baseline_sha, candidate_sha):
            if facts.blob_at(candidate_sha, path) == facts.blob_at(landed_at, path):
                continue
            # The blob may differ legitimately: other work touched the same file and merged cleanly. Recompute what a clean landing of the
            # candidate on the commit's first parent must contain and compare against that.
            if expected_tree is None:
                parent = facts.resolve(f"{landed_at}^")
                expected_tree = (facts.merge_trees(baseline_sha, parent, candidate_sha) or "") if parent else ""
            if not expected_tree or facts.blob_at(expected_tree, path) != facts.blob_at(landed_at, path):
                missing.append(path)
    elif not facts.is_ancestor(candidate_sha, landed_at):
        missing.append("<candidate not contained and no baseline to compare content>")
    checks: list[PostMergeCheck] = [PostMergeCheck("merge_reachable_from_integration_ref", reachable)]
    checks.append(PostMergeCheck("expected_content_landed", not missing, ", ".join(missing[:10])))
    if reachable and not missing:
        checks.extend(check() for check in post_merge_checks)
    verified = all(c.passed for c in checks)
    verdict = PostMergeVerdict(verified, integration_ref, main_sha, missing, checks)
    detail = {"checks": [{"check": c.name, "passed": c.passed, "detail": c.detail} for c in checks]}
    if verified:
        verdict.decision = AutonomyDecision(
            Condition.POST_MERGE_VERIFIED, POLICY, Action.MARK_DONE, task_id, {"integration_ref": integration_ref}, shas, detail
        )
    else:
        failed_content = bool(missing) or not reachable
        observed = {"integration_ref": integration_ref, "failed": ",".join(c.name for c in checks if not c.passed)}
        escalation = None
        if failed_content:
            # The work already landed (or the host says it did): building a replacement would duplicate it, and the evidence that the
            # landing differs from the validated candidate needs a human, so the question is specific.
            where = ", ".join(missing[:5]) or f"{landed_at[:7]} is not reachable from {integration_ref}"
            escalation = Escalation(
                EscalationReason.MERGED_CONTENT_NOT_VERIFIED,
                attempted=(
                    "compared the validated candidate's content with the commit that landed it",
                    "recomputed a clean landing of the candidate on that commit's parent to allow for concurrent changes",
                ),
                why_undeterminable=f"the landed commit {landed_at[:7]} differs from the validated candidate {candidate_sha[:7]} in: {where}",
                smallest_decision=f"Is the landed content of {where} the intended result, or should it be reverted and the work redone?",
            )
        verdict.decision = AutonomyDecision(
            Condition.POST_MERGE_VERIFICATION_FAILED,
            POLICY,
            Action.ESCALATE_TO_FOUNDER if failed_content else Action.REMEDIATE_CANDIDATE,
            task_id,
            observed,
            shas,
            {**detail, "task_not_done": True},
            escalation,
        )
    return verdict
