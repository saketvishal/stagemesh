"""Hosted-CI diagnosis: compare the candidate's CI with the base's CI before concluding anything about the candidate.

For every gate the diagnosis is one of: candidate regression, failure already present on base, broken/fragile test, infrastructure
failure, dependency (stacked base PR) failure, unsupported environment, or genuine unknown. A candidate is never declared broken
without base evidence, and no classification ever licenses touching unrelated code to turn CI green.

Broken tests are separated from production defects. A test that expects success from a provider that changed nothing while
production correctly reports `NO_IMPLEMENTATION_CHANGE` is a fixture mismatch: the fix is the test, and production code that owns
that behavior is protected from being "fixed" (`guard_remediation`).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ..diagnosis import normalize
from ..workspaces import NO_IMPLEMENTATION_CHANGE
from .decisions import Action, AutonomyDecision, Condition, Escalation, EscalationReason
from .scope import TaskScope

POLICY = "ci-diagnosis/v1"


class Conclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"
    PENDING = "pending"
    MISSING = "missing"

    @property
    def failed(self) -> bool:
        return self in {Conclusion.FAILURE, Conclusion.CANCELLED, Conclusion.TIMED_OUT}


class CIClass(StrEnum):
    PASSED = "PASSED"
    PENDING = "PENDING"
    CANDIDATE_REGRESSION = "CANDIDATE_REGRESSION"
    BASELINE_FAILURE = "BASELINE_FAILURE"
    BROKEN_FRAGILE_TEST = "BROKEN_FRAGILE_TEST"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    DEPENDENCY_BASE_PR_FAILURE = "DEPENDENCY_BASE_PR_FAILURE"
    UNSUPPORTED_ENVIRONMENT = "UNSUPPORTED_ENVIRONMENT"
    GENUINE_UNKNOWN = "GENUINE_UNKNOWN"


_CONDITION = {
    CIClass.PASSED: Condition.CI_GREEN,
    CIClass.PENDING: Condition.CI_PENDING,
    CIClass.CANDIDATE_REGRESSION: Condition.CI_CANDIDATE_REGRESSION,
    CIClass.BASELINE_FAILURE: Condition.CI_BASELINE_FAILURE,
    CIClass.BROKEN_FRAGILE_TEST: Condition.CI_BROKEN_FRAGILE_TEST,
    CIClass.INFRASTRUCTURE_FAILURE: Condition.CI_INFRASTRUCTURE_FAILURE,
    CIClass.DEPENDENCY_BASE_PR_FAILURE: Condition.CI_DEPENDENCY_BASE_PR_FAILURE,
    CIClass.UNSUPPORTED_ENVIRONMENT: Condition.CI_UNSUPPORTED_ENVIRONMENT,
    CIClass.GENUINE_UNKNOWN: Condition.CI_GENUINE_UNKNOWN,
}

def condition_for(klass: CIClass) -> Condition:
    return _CONDITION[klass]


_INFRA = re.compile(
    r"(runner (has )?received a shutdown signal|lost communication with the server|econnreset|"
    r"etimedout|connection reset by peer|no space left on device|503 service unavailable|502 bad gateway|"
    r"could not resolve host|temporary failure in name resolution|rate limit exceeded|"
    r"error: the process .* failed to start|unable to download action|failed to download action)",
    re.IGNORECASE,
)
_UNSUPPORTED = re.compile(
    r"(unsupported (python|node|platform|os|architecture)|requires python\s*[<>=!~]|no matching distribution found|"
    r"not supported on (windows|linux|macos)|platform .* is not supported|does not support (windows|linux|macos))",
    re.IGNORECASE,
)
_NO_DETAIL = "log:"  # the signature of a failure that exposes no log lines and no test names (hosted check runs carry no logs)
_KEY_LINE = re.compile(r"(fail|error|assert|exception|traceback|expected|denied|not found)", re.IGNORECASE)


@dataclass(frozen=True)
class GateOutcome:
    name: str
    conclusion: Conclusion
    log: str = ""
    failing_tests: tuple[str, ...] = ()
    rerun_conclusions: tuple[Conclusion, ...] = ()  # reruns of the very same SHA
    ref: str | None = None  # the host's identifier of this check run (what a rerun request needs)

    @property
    def signature(self) -> str:
        """Failure identity that survives SHAs, timings and temp paths: failing tests when known, else the key error lines."""
        if self.failing_tests:
            return "tests:" + ",".join(sorted(self.failing_tests))
        lines = sorted({normalize(line) for line in self.log.splitlines() if _KEY_LINE.search(line)})
        if not lines:
            return _NO_DETAIL
        # every distinct failure line takes part: truncating would let a new failure that sorts last hide behind an old red gate
        return f"log:{len(lines)}:{hashlib.sha256(chr(10).join(lines).encode('utf-8')).hexdigest()[:16]}"

    @property
    def infrastructure(self) -> bool:
        """Only an infrastructure *signature* in the log proves infrastructure. A cancellation or timeout alone proves nothing: a
        candidate that hangs looks exactly like that."""
        return not self.failing_tests and bool(_INFRA.search(self.log))

    @property
    def ambiguous_abort(self) -> bool:
        return not self.failing_tests and not _INFRA.search(self.log) and self.conclusion in {Conclusion.CANCELLED, Conclusion.TIMED_OUT}

    @property
    def unsupported_environment(self) -> bool:
        return not self.failing_tests and bool(_UNSUPPORTED.search(self.log))


@dataclass(frozen=True)
class HostedCIRun:
    sha: str
    gates: Mapping[str, GateOutcome]
    complete: bool = True
    environment: str = ""  # where the run executed (e.g. "github-actions", "local"); empty when unknown


class HostedCI(Protocol):
    """Connector boundary for hosted CI. Real implementations read GitHub check runs; tests use `FakeHostedCI`."""

    def run_for(self, sha: str) -> HostedCIRun | None:
        ...


class FakeHostedCI:
    def __init__(self, runs: Iterable[HostedCIRun] = ()):
        self.runs = {run.sha: run for run in runs}
        self.requests: list[str] = []

    def run_for(self, sha: str) -> HostedCIRun | None:
        self.requests.append(sha)
        return self.runs.get(sha)


@dataclass(frozen=True)
class ProductionInvariant:
    """Production behavior that is declared correct; a test that contradicts it is the broken party."""

    code: str
    description: str
    owner_paths: tuple[str, ...]


PRODUCTION_INVARIANTS: dict[str, ProductionInvariant] = {
    NO_IMPLEMENTATION_CHANGE: ProductionInvariant(
        NO_IMPLEMENTATION_CHANGE,
        "an implementation run that changes nothing is a failure, never a candidate",
        ("src/stagemesh/workspaces.py", "src/stagemesh/execution.py", "src/stagemesh/providers.py"),
    )
}


@dataclass(frozen=True)
class TestObservation:
    """What a failing test observed, as structured facts. Emitted by test harnesses via `STAGEMESH_TEST_OBSERVATION {json}` lines."""

    __test__ = False  # not a pytest test class

    test_id: str
    test_path: str
    expected: str  # "success" | "failure"
    observed_failure_code: str | None
    provider_made_change: bool


@dataclass(frozen=True)
class TestDefect:
    __test__ = False

    test_id: str
    test_path: str
    invariant: ProductionInvariant
    explanation: str


_MARKER = re.compile(r"STAGEMESH_TEST_OBSERVATION\s+(\{.*\})")


def observations_from_log(log: str) -> list[TestObservation]:
    found: list[TestObservation] = []
    for match in _MARKER.finditer(log):
        try:
            data = json.loads(match.group(1))
            found.append(
                TestObservation(
                    str(data["test_id"]),
                    str(data["test_path"]),
                    str(data["expected"]),
                    data.get("observed_failure_code"),
                    bool(data["provider_made_change"]),
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    return found


def detect_test_defect(observation: TestObservation) -> TestDefect | None:
    """A fixture mismatch: the test demands success from a no-op provider while production correctly reports the failure code."""
    invariant = PRODUCTION_INVARIANTS.get(observation.observed_failure_code or "")
    if invariant is None or observation.expected != "success" or observation.provider_made_change:
        return None
    return TestDefect(
        observation.test_id,
        observation.test_path,
        invariant,
        f"{observation.test_id} expects success from a provider that made no change, but production correctly returned "
        f"{invariant.code} ({invariant.description}); the fixture must make the provider change something or assert the failure",
    )


def guard_remediation(changed_files: Iterable[str], defects: Iterable[TestDefect]) -> list[str]:
    """Violations when a proposed remediation edits production code that a diagnosed test defect declares correct."""
    protected: dict[str, TestDefect] = {}
    for defect in defects:
        for path in defect.invariant.owner_paths:
            protected[path] = defect
    return [
        f"{path}: production behavior {protected[path].invariant.code} is correct; fix {protected[path].test_path} instead of weakening it"
        for path in (item.replace("\\", "/") for item in changed_files)
        if path in protected
    ]


@dataclass(frozen=True)
class GateDiagnosis:
    gate: str
    klass: CIClass
    reason: str
    candidate_signature: str = ""
    base_signature: str = ""
    new_failing_tests: tuple[str, ...] = ()
    test_defect: TestDefect | None = None
    evidence: str = "SIGNATURE"  # how candidate and base failures were compared: TEST_SET | SIGNATURE | GATE_LEVEL (conclusions only)

    def to_dict(self) -> dict[str, object]:
        return {
            "gate": self.gate,
            "class": self.klass.value,
            "reason": self.reason,
            "evidence": self.evidence,
            "candidate_signature": self.candidate_signature[:300],
            "base_signature": self.base_signature[:300],
            "new_failing_tests": list(self.new_failing_tests),
            "test_defect": self.test_defect.explanation if self.test_defect else None,
        }


def diagnose_gate(
    name: str,
    candidate: GateOutcome | None,
    base: GateOutcome | None,
    *,
    dependency: GateOutcome | None = None,
    defects: Iterable[TestDefect] = (),
    same_environment: bool = True,
) -> GateDiagnosis:
    if candidate is None or candidate.conclusion is Conclusion.MISSING:
        return GateDiagnosis(name, CIClass.PENDING, "the candidate has not reported this gate yet")
    if candidate.conclusion is Conclusion.PENDING:
        return GateDiagnosis(name, CIClass.PENDING, "the candidate's gate is still running")
    if candidate.conclusion in {Conclusion.SUCCESS, Conclusion.SKIPPED}:
        return GateDiagnosis(name, CIClass.PASSED, f"gate {candidate.conclusion.value}")

    signature = candidate.signature
    repeated = any(c.failed for c in candidate.rerun_conclusions)
    if (candidate.ambiguous_abort or candidate.infrastructure) and base is not None and base.conclusion is Conclusion.SUCCESS and same_environment and repeated:
        return GateDiagnosis(
            name, CIClass.CANDIDATE_REGRESSION, "the gate failed again on a rerun of the identical SHA while base is green: that is the candidate", signature
        )
    if candidate.ambiguous_abort and base is not None and base.conclusion is Conclusion.SUCCESS and Conclusion.SUCCESS not in candidate.rerun_conclusions:
        return GateDiagnosis(
                name, CIClass.GENUINE_UNKNOWN, "cancelled or timed out with no infrastructure signature: rerun once before judging", signature, evidence="ABORT_UNCONFIRMED"
            )
    defect = next((d for d in defects if d.test_id in candidate.failing_tests), None)
    if defect is not None:
        return GateDiagnosis(name, CIClass.BROKEN_FRAGILE_TEST, defect.explanation, signature, test_defect=defect)
    if Conclusion.SUCCESS in candidate.rerun_conclusions:
        return GateDiagnosis(name, CIClass.BROKEN_FRAGILE_TEST, "the gate passed on a rerun of the identical SHA: fragile or flaky", signature)

    base_failed = base is not None and base.conclusion.failed
    base_passed = base is not None and base.conclusion is Conclusion.SUCCESS
    base_signature = base.signature if base is not None and base_failed else ""

    if base is not None and not same_environment and (base_failed or base_passed):
        # Different failures in different environments prove nothing about the candidate: base must be rerun where the candidate ran.
        return GateDiagnosis(
            name,
            CIClass.GENUINE_UNKNOWN,
            "candidate and base ran in different environments and failed differently; base CI must be rerun in the candidate's environment",
            signature,
            base_signature,
            evidence="ENVIRONMENT_MISMATCH",
        )

    if base_passed:
        if dependency is not None and dependency.conclusion.failed and dependency.signature == signature:
            return GateDiagnosis(
                name, CIClass.DEPENDENCY_BASE_PR_FAILURE, "base passes, but the stacked dependency PR fails this gate identically", signature, dependency.signature
            )
        if candidate.infrastructure:
            return GateDiagnosis(name, CIClass.INFRASTRUCTURE_FAILURE, "failure matches an infrastructure signature and names no failing test", signature)
        return GateDiagnosis(name, CIClass.CANDIDATE_REGRESSION, "the gate passes on base and fails on the candidate", signature, new_failing_tests=candidate.failing_tests)

    if base_failed and base is not None:
        if signature == base_signature or (candidate.failing_tests and set(candidate.failing_tests) <= set(base.failing_tests)):
            strength = "TEST_SET" if candidate.failing_tests else ("GATE_LEVEL" if signature == _NO_DETAIL else "SIGNATURE")
            return GateDiagnosis(
                name,
                CIClass.BASELINE_FAILURE,
                "the gate fails identically on base: pre-existing, not introduced by the candidate",
                signature,
                base_signature,
                evidence=strength,
            )
        if candidate.infrastructure:
            return GateDiagnosis(name, CIClass.INFRASTRUCTURE_FAILURE, "failure differs from base and matches an infrastructure signature", signature, base_signature)
        # A failure that differs from base's is a change in how the gate fails. Even if both sides mention an environment error, the
        # difference may be the candidate's (a new failure line, a raised requires-python): it is a regression, never tolerated.
        new_tests = tuple(sorted(set(candidate.failing_tests) - set(base.failing_tests)))
        return GateDiagnosis(
            name, CIClass.CANDIDATE_REGRESSION, "the gate fails on base and the candidate fails it differently (new failures)", signature, base_signature, new_tests
        )

    # No usable base evidence: never conclude the candidate is broken.
    if dependency is not None and dependency.conclusion.failed and dependency.signature == signature:
        return GateDiagnosis(name, CIClass.DEPENDENCY_BASE_PR_FAILURE, "the stacked dependency PR fails this gate identically", signature, dependency.signature)
    if candidate.infrastructure:
        return GateDiagnosis(name, CIClass.INFRASTRUCTURE_FAILURE, "failure matches an infrastructure signature and names no failing test", signature)
    if candidate.unsupported_environment:
        return GateDiagnosis(
            name,
            CIClass.UNSUPPORTED_ENVIRONMENT,
            "failure looks like an unsupported-environment error, but without base CI the candidate cannot be ruled out as its cause",
            signature,
            evidence="NO_BASE",
        )
    return GateDiagnosis(name, CIClass.GENUINE_UNKNOWN, "no base CI evidence for this gate; cannot attribute the failure to the candidate", signature)


@dataclass(frozen=True)
class CIDiagnosis:
    candidate_sha: str
    base_sha: str | None
    gates: tuple[GateDiagnosis, ...]
    notes: tuple[str, ...] = ()

    def by_class(self, klass: CIClass) -> list[GateDiagnosis]:
        return [gate for gate in self.gates if gate.klass is klass]

    @property
    def overall(self) -> CIClass:
        for klass in (
            CIClass.CANDIDATE_REGRESSION,
            CIClass.PENDING,
            CIClass.BROKEN_FRAGILE_TEST,
            CIClass.DEPENDENCY_BASE_PR_FAILURE,
            CIClass.INFRASTRUCTURE_FAILURE,
            CIClass.GENUINE_UNKNOWN,
            CIClass.UNSUPPORTED_ENVIRONMENT,
            CIClass.BASELINE_FAILURE,
        ):
            if self.by_class(klass):
                return klass
        return CIClass.PASSED

    @property
    def defects(self) -> list[TestDefect]:
        return [gate.test_defect for gate in self.gates if gate.test_defect is not None]

    def merge_blockers(self, *, allow_baseline_failures: bool = True, baseline_requires_detail: bool = False) -> list[str]:
        """Gates whose state forbids merging; baseline and unsupported-environment failures pass only when policy allows.

        `baseline_requires_detail` additionally refuses to treat a gate as baseline when the two failures could only be compared by
        conclusion (no log lines or test names on either side), because a different failure of the same gate would look identical.
        """
        tolerated = {CIClass.PASSED}
        if allow_baseline_failures:
            tolerated |= {CIClass.BASELINE_FAILURE, CIClass.UNSUPPORTED_ENVIRONMENT}
        blockers = [f"{g.gate}: {g.klass.value} ({g.reason})" for g in self.gates if g.klass not in tolerated]
        # an environment error is tolerable only when base demonstrably fails the same way
        blockers += [
            f"{g.gate}: UNSUPPORTED_ENVIRONMENT not confirmed by base CI ({g.reason})"
            for g in self.by_class(CIClass.UNSUPPORTED_ENVIRONMENT)
            if g.evidence != "BASE_CONFIRMED" and CIClass.UNSUPPORTED_ENVIRONMENT in tolerated
        ]
        if allow_baseline_failures and baseline_requires_detail:
            blockers += [f"{g.gate}: BASELINE_FAILURE compared by gate conclusion only" for g in self.weak_baseline_gates()]
        return blockers

    def weak_baseline_gates(self) -> list[GateDiagnosis]:
        return [g for g in self.by_class(CIClass.BASELINE_FAILURE) if g.evidence == "GATE_LEVEL"]

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_sha": self.candidate_sha,
            "base_sha": self.base_sha,
            "overall": self.overall.value,
            "gates": [g.to_dict() for g in self.gates],
            "notes": list(self.notes),
        }


def diagnose_ci(
    candidate: HostedCIRun | None,
    base: HostedCIRun | None,
    *,
    dependency: HostedCIRun | None = None,
    candidate_sha: str | None = None,
    observations: Iterable[TestObservation] = (),
) -> CIDiagnosis:
    sha = candidate.sha if candidate is not None else (candidate_sha or "")
    defects = [d for d in (detect_test_defect(o) for o in observations) if d is not None]
    base_gates = base.gates if base is not None and base.complete else {}
    dep_gates = dependency.gates if dependency is not None else {}
    names = sorted(set(candidate.gates if candidate else ()) | set(base.gates if base else ()))
    same_environment = not (candidate and base and candidate.environment and base.environment and candidate.environment != base.environment)
    notes: list[str] = []
    if not same_environment:
        notes.append(f"candidate ran in {candidate.environment!r} but base in {base.environment!r}")  # type: ignore[union-attr]
    if base is None:
        notes.append("no base CI run available: failures cannot be attributed to the candidate")
    elif not base.complete:
        notes.append("base CI is still running")
    diagnoses = []
    for name in names:
        outcome = candidate.gates.get(name) if candidate else None
        if outcome is None and candidate is not None and candidate.complete:
            notes.append(f"gate {name} exists on base but was not reported for the candidate")
        diagnoses.append(
            diagnose_gate(
                name,
                outcome,
                base_gates.get(name),
                dependency=dep_gates.get(name),
                defects=defects,
                same_environment=same_environment,
            )
        )
    return CIDiagnosis(sha, base.sha if base is not None else None, tuple(diagnoses), tuple(notes))


def unresolved_ci_escalation(
    diagnosis: CIDiagnosis,
    klass: CIClass,
    condition: Condition,
    task_id: str | None,
    shas: dict[str, str],
    observed: dict[str, str],
    detail: dict[str, object],
    *,
    retried: bool,
) -> AutonomyDecision:
    """Bounded retries are spent and CI still cannot be attributed: that is a real policy question, so it is asked specifically."""
    gates = ", ".join(g.gate for g in diagnosis.by_class(klass))
    return AutonomyDecision(
        condition,
        POLICY,
        Action.ESCALATE_TO_FOUNDER,
        task_id,
        observed,
        shas,
        detail,
        Escalation(
            EscalationReason.CI_FAILURE_UNRESOLVED,
            attempted=(
                "compared the candidate's CI with base CI",
                *(("requested a rerun of the failed job on the same SHA and observed it fail again",) if retried else ()),
                "checked that the failure carries no sign of being caused by the candidate's own changes",
            ),
            why_undeterminable=f"gate(s) {gates} keep failing as {klass.value.lower().replace('_', ' ')} and cannot be attributed to the candidate or cleared by retrying",
            smallest_decision=f"May this candidate merge on its local validation and review evidence while hosted gate(s) {gates} stay unresolved?",
        ),
    )


def plan_ci_response(
    diagnosis: CIDiagnosis,
    *,
    task_id: str | None,
    scope: TaskScope | None = None,
    reruns_left: int = 1,
    rerun_supported: bool = True,
) -> AutonomyDecision:
    """Pure policy mapping a CI diagnosis to the single next action. Never an action that edits unrelated code."""
    overall = diagnosis.overall
    shas = {"candidate": diagnosis.candidate_sha, **({"base": diagnosis.base_sha} if diagnosis.base_sha else {})}
    observed = {klass.value.lower(): ",".join(g.gate for g in diagnosis.by_class(klass)) for klass in CIClass if diagnosis.by_class(klass)}
    detail: dict[str, object] = {"diagnosis": diagnosis.to_dict(), "forbid_unrelated_ci_fixes": True}
    condition = _CONDITION[overall]
    action: Action
    if overall is CIClass.PASSED:
        action = Action.PROCEED
    elif overall is CIClass.PENDING:
        action = Action.WAIT
    elif overall is CIClass.CANDIDATE_REGRESSION:
        action = Action.REMEDIATE_CANDIDATE
        detail["remediate_gates"] = [g.gate for g in diagnosis.by_class(CIClass.CANDIDATE_REGRESSION)]
        detail["not_to_fix"] = [g.gate for g in diagnosis.gates if g.klass is not CIClass.CANDIDATE_REGRESSION and g.klass is not CIClass.PASSED]
    elif overall is CIClass.BROKEN_FRAGILE_TEST:
        defects = diagnosis.defects
        detail["production_code_protected"] = sorted({p for d in defects for p in d.invariant.owner_paths})
        in_scope = scope is not None and defects and all(scope.permits(d.test_path) for d in defects)
        if in_scope:
            action = Action.FIX_TEST_FIXTURE
            detail["fix_tests"] = sorted({d.test_path for d in defects})
        elif defects:
            # A real test defect this task may not touch still blocks the merge: ask the one specific question instead of stalling.
            detail["deferred_because"] = "the broken test is outside this task's allowed files"
            return unresolved_ci_escalation(diagnosis, CIClass.BROKEN_FRAGILE_TEST, condition, task_id, shas, observed, detail, retried=False)
        elif reruns_left > 0 and rerun_supported:
            action = Action.RERUN_CI
        else:
            return unresolved_ci_escalation(diagnosis, CIClass.BROKEN_FRAGILE_TEST, condition, task_id, shas, observed, detail, retried=rerun_supported)
    elif overall is CIClass.INFRASTRUCTURE_FAILURE:
        detail["reruns_left"] = reruns_left
        if reruns_left > 0 and rerun_supported:
            action = Action.RERUN_CI
        else:
            return unresolved_ci_escalation(diagnosis, CIClass.INFRASTRUCTURE_FAILURE, condition, task_id, shas, observed, detail, retried=rerun_supported)
    elif overall is CIClass.DEPENDENCY_BASE_PR_FAILURE:
        action = Action.BLOCK_ON_DEPENDENCY
    elif overall is CIClass.GENUINE_UNKNOWN:
        mismatch = any(g.evidence == "ENVIRONMENT_MISMATCH" for g in diagnosis.gates)
        if diagnosis.base_sha is None or mismatch:
            action = Action.REQUEST_BASE_CI
        elif reruns_left > 0 and rerun_supported:
            action = Action.RERUN_CI
        else:
            return unresolved_ci_escalation(diagnosis, CIClass.GENUINE_UNKNOWN, condition, task_id, shas, observed, detail, retried=rerun_supported)
    elif overall is CIClass.UNSUPPORTED_ENVIRONMENT:
        unconfirmed = [g.gate for g in diagnosis.by_class(CIClass.UNSUPPORTED_ENVIRONMENT) if g.evidence != "BASE_CONFIRMED"]
        action = Action.REQUEST_BASE_CI if unconfirmed else Action.RECORD_AND_DEFER
        if unconfirmed:
            detail["unconfirmed_by_base"] = unconfirmed
    else:  # BASELINE_FAILURE
        action = Action.RECORD_BASELINE_FAILURE_AND_PROCEED
        detail["deferred_baseline_failures"] = [g.gate for g in diagnosis.by_class(CIClass.BASELINE_FAILURE)]
        if diagnosis.weak_baseline_gates():
            detail["weak_baseline_evidence"] = [g.gate for g in diagnosis.weak_baseline_gates()]  # compared by conclusion only
    return AutonomyDecision(condition, POLICY, action, task_id, observed, shas, detail)
