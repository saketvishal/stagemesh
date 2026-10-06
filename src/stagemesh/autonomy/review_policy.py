"""Independent-review lifecycle policy.

A review is evidence about exactly one candidate SHA. This module classifies a review report against the task's scope:

* blocking, in scope      -> remediated automatically (and only these are put in the remediation brief);
* blocking, out of scope  -> deferred and recorded, unless it is tied to an acceptance criterion, which needs a founder decision;
* non-blocking            -> recorded, never remediated;
* unrelated suggestion    -> recorded as deferred work; the candidate's scope does not change;
* test defect             -> classified separately: fixed only when the test belongs to the objective.

Any remediation that produces a new SHA invalidates the previous validation and review (`required_after_remediation`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from .decisions import Action, AutonomyDecision, Condition, Escalation, EscalationReason
from .scope import DeferredItem, TaskScope

POLICY = "review/v1"
_BLOCKING_SEVERITIES = frozenset({"blocker", "critical", "high", "major", "error"})


class FindingClass(StrEnum):
    BLOCKING_IN_SCOPE = "BLOCKING_IN_SCOPE"
    BLOCKING_OUT_OF_SCOPE = "BLOCKING_OUT_OF_SCOPE"
    NON_BLOCKING = "NON_BLOCKING"
    UNRELATED_SUGGESTION = "UNRELATED_SUGGESTION"
    TEST_DEFECT = "TEST_DEFECT"


@dataclass(frozen=True)
class ReviewFindingInput:
    message: str
    severity: str = "error"
    path: str | None = None
    blocking: bool | None = None  # explicit reviewer verdict; falls back to severity
    acceptance_criterion: str | None = None  # set when the finding says an acceptance criterion is unmet
    category: str | None = None  # "test_defect" | "suggestion" | ...

    @property
    def is_blocking(self) -> bool:
        if self.blocking is not None:
            return self.blocking
        return self.severity.casefold() in _BLOCKING_SEVERITIES


@dataclass(frozen=True)
class ClassifiedFinding:
    finding: ReviewFindingInput
    klass: FindingClass
    reason: str


@dataclass(frozen=True)
class ReviewReport:
    reviewed_sha: str
    reviewer_provider: str | None
    implementer_provider: str | None
    findings: tuple[ReviewFindingInput, ...] = ()
    execution_invoked: bool = True


@dataclass
class ReviewAssessment:
    decision: AutonomyDecision
    classified: list[ClassifiedFinding] = field(default_factory=list)
    remediate: list[ReviewFindingInput] = field(default_factory=list)
    fix_tests: list[ReviewFindingInput] = field(default_factory=list)
    deferred: list[DeferredItem] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        """The review authorizes the candidate: nothing blocking remains (suggestions are recorded, not fixed)."""
        return self.decision.condition in {Condition.REVIEW_APPROVED, Condition.REVIEW_UNRELATED_SUGGESTION}

    def remediation_brief(self) -> str:
        """Prompt text for the implementation agent: only in-scope blocking findings, with the explicit non-goals."""
        lines = ["Address ONLY these blocking findings; do not change anything else:"]
        lines += [f"- {f.message}" + (f" ({f.path})" if f.path else "") for f in (*self.remediate, *self.fix_tests)]
        if self.deferred:
            lines.append("Explicitly deferred (do NOT fix):")
            lines += [f"- {item.summary}" for item in self.deferred]
        return "\n".join(lines)


def classify_finding(finding: ReviewFindingInput, scope: TaskScope) -> ClassifiedFinding:
    in_scope = finding.path is None or scope.permits(finding.path)
    if finding.category == "test_defect":
        return ClassifiedFinding(finding, FindingClass.TEST_DEFECT, "reviewer identified a test problem, not a production defect")
    if finding.category == "suggestion":
        return ClassifiedFinding(finding, FindingClass.UNRELATED_SUGGESTION, "reviewer suggestion, not a defect in the objective")
    if finding.is_blocking:
        if in_scope:
            return ClassifiedFinding(finding, FindingClass.BLOCKING_IN_SCOPE, "blocking defect in files this task may change")
        return ClassifiedFinding(finding, FindingClass.BLOCKING_OUT_OF_SCOPE, f"blocking, but {finding.path} is outside this task's scope")
    if not in_scope:
        return ClassifiedFinding(finding, FindingClass.UNRELATED_SUGGESTION, f"non-blocking and {finding.path} is outside this task's scope")
    return ClassifiedFinding(finding, FindingClass.NON_BLOCKING, "non-blocking observation")


def required_after_remediation(old_candidate: str, new_candidate: str | None) -> list[str]:
    """Evidence that must be re-collected when remediation yields `new_candidate`; empty when it produced no new SHA."""
    if not new_candidate or new_candidate == old_candidate:
        return []
    return ["VALIDATION", "REVIEW"]


def assess_review(
    report: ReviewReport,
    *,
    candidate_sha: str,
    scope: TaskScope,
    task_id: str | None,
    independent_required: bool = True,
) -> ReviewAssessment:
    shas = {"candidate": candidate_sha, "review": report.reviewed_sha}
    observed = {"reviewer": report.reviewer_provider or "unknown", "implementer": report.implementer_provider or "unknown"}

    def done(condition: Condition, action: Action, assessment_kwargs: dict[str, object] | None = None, escalation: Escalation | None = None, **detail: object) -> ReviewAssessment:
        decision = AutonomyDecision(condition, POLICY, action, task_id, observed, shas, dict(detail), escalation)
        return ReviewAssessment(decision, **(assessment_kwargs or {}))  # type: ignore[arg-type]

    if report.reviewed_sha != candidate_sha:
        return done(Condition.REVIEW_STALE_FOR_CANDIDATE, Action.REQUIRE_RE_REVIEW, reason="review is bound to a different SHA than the candidate")
    if independent_required and not _independent(report):
        return done(
            Condition.REVIEW_NOT_INDEPENDENT,
            Action.REQUIRE_INDEPENDENT_REVIEW,
            reason="reviewer is the implementation provider, unknown, or never actually executed",
        )

    classified = [classify_finding(f, scope) for f in report.findings]
    remediate = [c.finding for c in classified if c.klass is FindingClass.BLOCKING_IN_SCOPE]
    fix_tests = [c.finding for c in classified if c.klass is FindingClass.TEST_DEFECT and (c.finding.path is None or scope.permits(c.finding.path))]
    deferred = [
        DeferredItem(c.finding.message, "review", c.finding.path, report.reviewed_sha)
        for c in classified
        if c.klass in {FindingClass.UNRELATED_SUGGESTION, FindingClass.BLOCKING_OUT_OF_SCOPE, FindingClass.NON_BLOCKING}
        or (c.klass is FindingClass.TEST_DEFECT and c.finding not in fix_tests)
    ]
    kwargs: dict[str, object] = {"classified": classified, "remediate": remediate, "fix_tests": fix_tests, "deferred": deferred}
    counts = {klass.value.lower(): str(sum(1 for c in classified if c.klass is klass)) for klass in FindingClass}
    observed.update({key: value for key, value in counts.items() if value != "0"})

    criterion = next((c.finding for c in classified if c.klass is FindingClass.BLOCKING_OUT_OF_SCOPE and c.finding.acceptance_criterion), None)
    if criterion is not None:
        escalation = Escalation(
            EscalationReason.SCOPE_EXTENSION_REQUIRED_BY_ACCEPTANCE_CRITERION,
            attempted=("classified the finding against the task's allowed and forbidden files", "confirmed the candidate cannot satisfy it within scope"),
            why_undeterminable=f"acceptance criterion {criterion.acceptance_criterion!r} can only be met by changing {criterion.path}, which the task forbids",
            smallest_decision=f"Allow this task to change {criterion.path}, or drop acceptance criterion {criterion.acceptance_criterion!r}?",
        )
        return done(Condition.REVIEW_BLOCKING_IN_SCOPE, Action.ESCALATE_TO_FOUNDER, kwargs, escalation, finding=criterion.message)
    if remediate or fix_tests:
        decision = done(
            Condition.REVIEW_BLOCKING_IN_SCOPE,
            Action.REMEDIATE_CANDIDATE,
            kwargs,
            remediate=[f.message for f in remediate],
            fix_tests=[f.message for f in fix_tests],
            deferred=[d.summary for d in deferred],
            invalidates=required_after_remediation(candidate_sha, "<new-sha>"),
        )
        return decision
    if any(c.klass is FindingClass.UNRELATED_SUGGESTION for c in classified):
        return done(Condition.REVIEW_UNRELATED_SUGGESTION, Action.RECORD_AND_DEFER, kwargs, deferred=[d.summary for d in deferred], scope_unchanged=True)
    return done(Condition.REVIEW_APPROVED, Action.PROCEED, kwargs, recorded_non_blocking=[d.summary for d in deferred])


def _independent(report: ReviewReport) -> bool:
    reviewer, implementer = report.reviewer_provider, report.implementer_provider
    return bool(
        report.execution_invoked
        and reviewer
        and implementer
        and reviewer.strip().casefold() != implementer.strip().casefold()
    )


def drain_deferred(assessments: Iterable[ReviewAssessment]) -> list[DeferredItem]:
    return [item for assessment in assessments for item in assessment.deferred]
