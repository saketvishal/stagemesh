"""PR dependency and stacked-PR policy.

A PR may declare that it depends on another PR, or be stacked implicitly (its base branch is another open PR's head branch).
Downstream work blocks itself until every dependency has landed; a dependency that is red or unmergeable keeps it blocked and is
reported so that PR can be remediated; once the dependency lands the downstream PR is refreshed against the new base and resumes
validation and review without anyone naming it. Evaluation is a pure function of observed PR facts: nothing polls or sleeps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .decisions import Action, AutonomyDecision, Condition, Escalation, EscalationReason

POLICY = "pr-dependencies/v1"


class PRState(StrEnum):
    OPEN = "OPEN"
    MERGED = "MERGED"
    CLOSED = "CLOSED"


class CIRollup(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PENDING = "PENDING"


@dataclass(frozen=True)
class PullRequest:
    number: int
    head_sha: str
    head_ref: str
    base_ref: str
    state: PRState = PRState.OPEN
    ci: CIRollup = CIRollup.PENDING
    mergeable: bool | None = None  # None: the host has not computed it yet
    merge_commit_sha: str | None = None


class PullRequestAdapter(Protocol):
    """Connector boundary for pull requests (GitHub in production; `FakePullRequests` in tests)."""

    def get(self, number: int) -> PullRequest | None:
        ...

    def set_base(self, number: int, base_ref: str) -> PullRequest:
        ...

    def find_by_head(self, head_ref: str) -> PullRequest | None:
        """The most recent PR (any state) whose head branch is `head_ref`: the parent of a PR stacked on that branch."""
        ...

    def merge(self, number: int, expected_head_sha: str, method: str = "squash") -> MergeOutcome:
        ...


class PullRequestPublisher(Protocol):
    """Opening and updating the PR for a candidate branch (idempotent: an existing open PR for the branch is edited, not duplicated)."""

    def find_open(self, head_ref: str) -> PullRequest | None:
        ...

    def open_pr(self, head_ref: str, base_ref: str, title: str, body: str, head_sha: str | None = None) -> PullRequest:
        ...

    def edit(self, number: int, title: str, body: str) -> PullRequest:
        ...


@dataclass(frozen=True)
class MergeOutcome:
    merged: bool
    sha: str | None = None
    reason: str = ""


class FakePullRequests:
    def __init__(self, prs: Iterable[PullRequest] = (), on_merge: Callable[[PullRequest, str], str | None] | None = None):
        self.prs: dict[int, PullRequest] = {pr.number: pr for pr in prs}
        self.calls: list[tuple[str, int, str]] = []
        self.on_merge = on_merge  # lands the PR in the test's git repository and returns the resulting commit SHA
        self.bodies: dict[int, tuple[str, str]] = {}
        self.default_mergeable: bool | None = None  # what the host reports for a newly opened PR (None: not computed yet)

    def get(self, number: int) -> PullRequest | None:
        self.calls.append(("get", number, ""))
        return self.prs.get(number)

    def set_base(self, number: int, base_ref: str) -> PullRequest:
        self.calls.append(("set_base", number, base_ref))
        current = self.prs[number]
        self.prs[number] = PullRequest(
            number, current.head_sha, current.head_ref, base_ref, current.state, current.ci, current.mergeable, current.merge_commit_sha
        )
        return self.prs[number]

    def update(self, pr: PullRequest) -> None:
        self.prs[pr.number] = pr

    def find_by_head(self, head_ref: str) -> PullRequest | None:
        self.calls.append(("find_by_head", 0, head_ref))
        matches = [pr for pr in self.prs.values() if pr.head_ref == head_ref]
        return max(matches, key=lambda pr: pr.number) if matches else None

    def find_open(self, head_ref: str) -> PullRequest | None:
        self.calls.append(("find_open", 0, head_ref))
        return next((pr for pr in self.prs.values() if pr.head_ref == head_ref and pr.state is PRState.OPEN), None)

    def open_pr(self, head_ref: str, base_ref: str, title: str, body: str, head_sha: str | None = None) -> PullRequest:
        self.calls.append(("open_pr", 0, head_ref))
        number = max(self.prs, default=0) + 1
        pr = PullRequest(number, head_sha or "", head_ref, base_ref, PRState.OPEN, CIRollup.PENDING, self.default_mergeable)
        self.prs[number] = pr
        self.bodies[number] = (title, body)
        return pr

    def edit(self, number: int, title: str, body: str) -> PullRequest:
        self.calls.append(("edit", number, title))
        self.bodies[number] = (title, body)
        return self.prs[number]

    def merge(self, number: int, expected_head_sha: str, method: str = "squash") -> MergeOutcome:
        self.calls.append(("merge", number, expected_head_sha))
        pr = self.prs[number]
        if pr.head_sha != expected_head_sha:  # what GitHub does with `sha` on the merge request: 409, nothing merged
            return MergeOutcome(False, None, "head_sha_mismatch")
        sha = self.on_merge(pr, method) if self.on_merge else None
        self.prs[number] = PullRequest(number, pr.head_sha, pr.head_ref, pr.base_ref, PRState.MERGED, pr.ci, pr.mergeable, sha)
        return MergeOutcome(True, sha)


@dataclass(frozen=True)
class PRDependency:
    pr: int
    depends_on: int


def implicit_dependencies(prs: Mapping[int, PullRequest]) -> set[PRDependency]:
    """Stacked PRs: an open PR whose base branch is another PR's head branch depends on that PR.

    A dependency that has merged still counts until the downstream PR is retargeted off its branch, so the landing is observed.
    """
    heads = {pr.head_ref: pr.number for pr in prs.values() if pr.state in {PRState.OPEN, PRState.MERGED}}
    return {PRDependency(pr.number, heads[pr.base_ref]) for pr in prs.values() if pr.state is PRState.OPEN and pr.base_ref in heads and heads[pr.base_ref] != pr.number}


def find_cycle(edges: Iterable[PRDependency]) -> list[int] | None:
    graph: dict[int, set[int]] = {}
    for edge in edges:
        graph.setdefault(edge.pr, set()).add(edge.depends_on)
    visiting: list[int] = []
    done: set[int] = set()

    def visit(node: int) -> list[int] | None:
        if node in visiting:
            return visiting[visiting.index(node) :] + [node]
        if node in done:
            return None
        visiting.append(node)
        for nxt in sorted(graph.get(node, ())):
            cycle = visit(nxt)
            if cycle:
                return cycle
        visiting.pop()
        done.add(node)
        return None

    for start in sorted(graph):
        cycle = visit(start)
        if cycle:
            return cycle
    return None


@dataclass
class DependencyAssessment:
    pr: int
    decision: AutonomyDecision
    blocked_on: list[int] = field(default_factory=list)
    red: list[int] = field(default_factory=list)  # dependencies that need remediation before anything downstream can move
    landed: list[int] = field(default_factory=list)
    refresh_required: bool = False
    retarget_base_to: str | None = None

    @property
    def blocked(self) -> bool:
        return self.decision.action is Action.BLOCK_ON_DEPENDENCY

    @property
    def can_resume(self) -> bool:
        return self.decision.action is Action.RESUME_AFTER_DEPENDENCY


def evaluate_dependencies(
    pr_number: int,
    prs: Mapping[int, PullRequest | None],
    declared: Iterable[PRDependency] = (),
    *,
    integration_ref: str = "main",
    was_blocked: bool = False,
    task_id: str | None = None,
) -> DependencyAssessment:
    pr = prs.get(pr_number)
    known = {n: p for n, p in prs.items() if p is not None}
    edges = {*declared, *implicit_dependencies(known)}
    cycle = find_cycle(edges)
    shas = {"pr_head": pr.head_sha} if pr is not None else {}
    if cycle is not None and pr_number in cycle:
        decision = AutonomyDecision(
            Condition.DEPENDENCY_CYCLE,
            POLICY,
            Action.ESCALATE_TO_FOUNDER,
            task_id,
            {"pr": str(pr_number), "cycle": "->".join(f"#{n}" for n in cycle)},
            shas,
            {},
            Escalation(
                EscalationReason.DEPENDENCY_CYCLE,
                attempted=("built the PR dependency graph from declared dependencies and stacked base branches",),
                why_undeterminable="the dependencies are circular, so there is no landing order that satisfies all of them",
                smallest_decision=f"Which of {', '.join(f'#{n}' for n in sorted(set(cycle)))} should land first (i.e. which dependency edge is wrong)?",
            ),
        )
        return DependencyAssessment(pr_number, decision)

    deps = sorted(edge.depends_on for edge in edges if edge.pr == pr_number)
    if pr is None or not deps:
        decision = AutonomyDecision(Condition.DEPENDENCY_LANDED, POLICY, Action.PROCEED, task_id, {"pr": str(pr_number), "dependencies": "none"}, shas)
        return DependencyAssessment(pr_number, decision)

    blocked_on: list[int] = []
    red: list[int] = []
    landed: list[int] = []
    for number in deps:
        dep = prs.get(number)
        if dep is None:
            blocked_on.append(number)  # unknown is not resolved: fail closed
        elif dep.state is PRState.MERGED and dep.base_ref == integration_ref:
            landed.append(number)
        elif dep.state is PRState.MERGED:
            blocked_on.append(number)  # merged into some other branch: it has not landed on the integration branch yet
        elif dep.state is PRState.CLOSED:
            return DependencyAssessment(pr_number, _closed_decision(pr_number, number, task_id, shas), blocked_on=[number])
        else:
            blocked_on.append(number)
            if dep.ci is CIRollup.FAILURE or dep.mergeable is False:
                red.append(number)

    observed = {"pr": str(pr_number), "depends_on": ",".join(f"#{n}" for n in deps)}
    if red:
        observed["red_dependencies"] = ",".join(f"#{n}" for n in red)
        decision = AutonomyDecision(
            Condition.DEPENDENCY_RED,
            POLICY,
            Action.BLOCK_ON_DEPENDENCY,
            task_id,
            observed,
            shas,
            {"resume": "automatic once every dependency has landed", "remediate_dependencies": red},
        )
        return DependencyAssessment(pr_number, decision, blocked_on=blocked_on, red=red, landed=landed)
    if blocked_on:
        observed["waiting_on"] = ",".join(f"#{n}" for n in blocked_on)
        decision = AutonomyDecision(
            Condition.DEPENDENCY_PENDING,
            POLICY,
            Action.BLOCK_ON_DEPENDENCY,
            task_id,
            observed,
            shas,
            {"resume": "automatic once every dependency has landed"},
        )
        return DependencyAssessment(pr_number, decision, blocked_on=blocked_on, landed=landed)

    retarget = integration_ref if pr.base_ref != integration_ref else None
    observed["landed"] = ",".join(f"#{n}" for n in landed)
    decision = AutonomyDecision(
        Condition.DEPENDENCY_LANDED,
        POLICY,
        Action.RESUME_AFTER_DEPENDENCY if was_blocked else Action.PROCEED,
        task_id,
        observed,
        shas,
        {"refresh_candidate": True, "retarget_base_to": retarget, "revalidate": True, "rereview": True},
    )
    return DependencyAssessment(pr_number, decision, landed=landed, refresh_required=True, retarget_base_to=retarget)


def _closed_decision(pr_number: int, dep: int, task_id: str | None, shas: dict[str, str]) -> AutonomyDecision:
    return AutonomyDecision(
        Condition.DEPENDENCY_CLOSED_WITHOUT_LANDING,
        POLICY,
        Action.ESCALATE_TO_FOUNDER,
        task_id,
        {"pr": str(pr_number), "closed_dependency": f"#{dep}"},
        shas,
        {},
        Escalation(
            EscalationReason.DEPENDENCY_CLOSED_WITHOUT_LANDING,
            attempted=(f"checked whether #{dep} merged (it did not)", "looked for a replacement PR carrying the same change"),
            why_undeterminable=f"#{pr_number} needs the work in #{dep}, which was closed without landing; whether that work is still wanted is a product decision",
            smallest_decision=f"Should #{dep} be re-landed (reopen it), or should #{pr_number} be rebuilt without it?",
        ),
    )


def load_pull_requests(adapter: PullRequestAdapter, numbers: Iterable[int]) -> dict[int, PullRequest | None]:
    return {number: adapter.get(number) for number in numbers}
