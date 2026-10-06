"""Deliver a validated, independently reviewed candidate: publish it as a branch and PR, observe hosted CI, decide merge-readiness.

This is one bounded pass, not an unattended loop: it never polls past its deadline, never merges unless explicitly told to and the
merge policy is satisfied, and every outcome is a typed status plus recorded decisions:

* `NOT_PUBLISHED`        the candidate's evidence does not authorize delivery (nothing leaves the machine)
* `NEEDS_REVALIDATION`   the base moved: a replacement candidate was created and must be validated and reviewed again (nothing is published)
* `PUBLISHED_WAITING`    PR open, hosted CI not finished within the deadline
* `PUBLISHED_REMEDIATING` CI shows a candidate regression (or a broken test of this task): the task was sent back for remediation
* `PUBLISHED_NOT_READY`  a merge-policy condition is unsatisfied (reasons listed)
* `MERGE_READY`          every merge-policy condition holds; the merge was not performed unless `merge=True`
* `MERGED`               performed and verified
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..domain import EvidenceKind, EvidenceStatus
from .ci_diagnosis import HostedCI
from .decisions import Action, AutonomyDecision, Condition
from .dependencies import PullRequestAdapter, PullRequestPublisher
from .github_adapter import GitHubAuthorizationError, GitHubRateLimited, authorization_escalation
from .supervisor import Supervisor

REMEDIATION_ACTIONS = {Action.REMEDIATE_CANDIDATE, Action.FIX_TEST_FIXTURE}


class Pulls(PullRequestAdapter, PullRequestPublisher, Protocol):
    """What delivery needs from the host: read, publish, retarget, merge."""


@dataclass
class DeliveryReport:
    task_id: str
    status: str
    candidate_sha: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    ci: dict[str, Any] | None = None
    unsatisfied: list[str] = field(default_factory=list)
    recommendation: str = ""
    merge_performed: bool = False
    decisions: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def to_markdown(self) -> str:
        lines = [f"# StageMesh delivery report: {self.task_id}", "", f"**Status:** {self.status}", f"**Recommendation:** {self.recommendation}", ""]
        for key in ("candidate_sha", "branch", "pr_number", "base_ref", "base_sha"):
            lines.append(f"- {key}: `{getattr(self, key)}`")
        lines.append(f"- merge performed: {self.merge_performed}")
        if self.evidence:
            lines += ["", "## Evidence bound to the exact candidate", *[f"- {k}: {v}" for k, v in self.evidence.items()]]
        if self.ci:
            lines += ["", "## Hosted CI (candidate compared with base)", *[f"- {g['gate']}: {g['class']} ({g['reason']})" for g in self.ci.get("gates", [])]]
        if self.unsatisfied:
            lines += ["", "## Unsatisfied merge conditions", *[f"- {item}" for item in self.unsatisfied]]
        lines += ["", "## Decision trace", *[f"- `{line}`" for line in self.decisions]]
        return "\n".join(lines) + "\n"


def branch_name(task_id: str, sha: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip("-.").lower() or "task"
    return f"stagemesh/{slug}-{sha[:7]}"


def evidence_summary(supervisor: Supervisor, task_id: str, sha: str) -> dict[str, Any]:
    store = supervisor.store
    summary: dict[str, Any] = {}
    for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW):
        row = store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=? ORDER BY created_at DESC LIMIT 1",
            (task_id, sha, kind, EvidenceStatus.PASSED),
        ).fetchone()
        if row is None:
            continue
        payload = json.loads(row["payload"])
        if kind is EvidenceKind.VALIDATION:
            summary["validation"] = f"PASSED on {sha[:7]}; gates: {', '.join(g['name'] for g in payload.get('gates', [])) or 'none recorded'}"
        else:
            summary["review"] = (
                f"PASSED on {sha[:7]} by {payload.get('review_provider')} (implementer {payload.get('implementer_provider')}; "
                f"independent={payload.get('independent_reviewer')})"
            )
    return summary


def pr_body(task_id: str, title: str, sha: str, evidence: dict[str, Any]) -> str:
    lines = [
        f"Task `{task_id}`: {title}",
        "",
        f"Candidate `{sha}`, produced under the StageMesh Founder Hands-Off supervisor in an isolated worktree.",
        "",
        "Evidence bound to this exact commit:",
        *[f"- {k}: {v}" for k, v in evidence.items()],
        "",
        "Automatic merge is disabled for this task; the supervisor reports merge-readiness separately.",
    ]
    return "\n".join(lines)


_PUSH_DENIED = re.compile(
    r"(authentication failed|permission denied|permission to \S+ denied|could not read username|access denied|not authorized|^(?:remote|fatal|error):.*\b403\b)",
    re.IGNORECASE | re.MULTILINE,
)


class BaseUnobservable(RuntimeError):
    """The remote base could not be fetched (network or host problem that is not an authorization problem)."""


class PushDenied(RuntimeError):
    """`git push` was refused for authorization reasons (not because the branch exists at a different commit)."""


def deliver(
    supervisor: Supervisor,
    task_id: str,
    *,
    remote: str,
    base: str,
    pulls: Pulls,
    ci: HostedCI,
    title: str,
    wait_seconds: float = 900.0,
    poll_seconds: float = 20.0,
    merge: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> DeliveryReport:
    """One bounded delivery pass. `supervisor.integration_ref` must be the remote base ref (refs/remotes/<remote>/<base>).

    Host authorization problems become a typed escalation (`ESCALATED`); rate limits become a wait. Neither raises.
    """
    report = DeliveryReport(task_id, "NOT_PUBLISHED", base_ref=f"{remote}/{base}")
    marker = supervisor.trace_marker()
    try:
        return _deliver_pass(supervisor, task_id, report, marker, remote, base, pulls, ci, title, wait_seconds, poll_seconds, merge, clock, sleep)
    except BaseUnobservable as error:
        supervisor.record(
            AutonomyDecision(
                Condition.CI_PENDING, "delivery/v1", Action.WAIT, task_id, {"reason": "base_unobservable"}, {}, {"fetch_error": str(error)[:200]}
            )
        )
        report.recommendation = "the remote could not be reached; nothing was published and delivery will be retried"
        report.decisions = [d["trace"] for d in supervisor.trace_after(task_id, marker)]
        return report
    except (GitHubAuthorizationError, GitHubRateLimited, PushDenied) as error:
        if isinstance(error, GitHubRateLimited):
            decision = AutonomyDecision(
                Condition.CI_PENDING, "delivery/v1", Action.WAIT, task_id, {"reason": "github_rate_limited"}, {}, {"retry_after_seconds": error.retry_after}
            )
            report.status = "PUBLISHED_WAITING" if report.pr_number else "NOT_PUBLISHED"
            report.recommendation = "GitHub rate limit reached; retry later"
        else:
            denied = error if isinstance(error, GitHubAuthorizationError) else GitHubAuthorizationError(403, str(error))
            decision = AutonomyDecision(
                Condition.MERGE_POLICY_UNSATISFIED,
                "delivery/v1",
                Action.ESCALATE_TO_FOUNDER,
                task_id,
                {"github_status": str(denied.status), "branch": report.branch or "none"},
                {"candidate": report.candidate_sha or "none"},
                {},
                authorization_escalation(denied, "permission to push branches and open pull requests on this repository"),
            )
            report.status = "ESCALATED"
            report.recommendation = f"escalated: {decision.escalation.reason.value}"  # type: ignore[union-attr]
        supervisor.record(decision)
        report.decisions = [d["trace"] for d in supervisor.trace_after(task_id, marker)]
        return report


def _sync_base(supervisor: Supervisor, report: DeliveryReport, remote: str, base: str) -> tuple[str | None, tuple[str, str] | None]:
    """Fetch the base and reconcile the candidate against it. Returns the base SHA and, when delivery must stop, (status, recommendation)."""
    fetched = supervisor.facts.git.run("fetch", "--quiet", remote, base, check=False)
    if fetched.returncode != 0:
        if _PUSH_DENIED.search(fetched.stderr):
            raise PushDenied(fetched.stderr.strip()[:300])
        raise BaseUnobservable(fetched.stderr.strip())
    base_sha = supervisor.facts.resolve(supervisor.integration_ref)
    report.base_sha = base_sha
    refresh = supervisor.reconcile_base(report.task_id)
    if refresh is not None and refresh.action in {Action.REFRESH_CANDIDATE, Action.CREATE_RETARGETED_CANDIDATE, Action.RECONSTRUCT_ON_NEW_BASE}:
        report.candidate_sha = str(supervisor.store.latest_candidate(report.task_id)["sha"])
        return base_sha, ("NEEDS_REVALIDATION", f"{base} advanced; the candidate was refreshed to a new SHA that must be validated and reviewed again")
    if refresh is not None and refresh.requires_human:
        return base_sha, ("NOT_PUBLISHED", f"escalated: {refresh.escalation.reason.value}")  # type: ignore[union-attr]
    return base_sha, None


def _deliver_pass(
    supervisor: Supervisor,
    task_id: str,
    report: DeliveryReport,
    marker: int,
    remote: str,
    base: str,
    pulls: Pulls,
    ci: HostedCI,
    title: str,
    wait_seconds: float,
    poll_seconds: float,
    merge: bool,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> DeliveryReport:
    facts = supervisor.facts

    def finish(status: str, recommendation: str) -> DeliveryReport:
        report.status, report.recommendation = status, recommendation
        report.decisions = [d["trace"] for d in supervisor.trace_after(task_id, marker)]
        return report

    prov = supervisor.provenance(task_id)
    report.candidate_sha = prov.candidate_sha
    problems = prov.evidence_problems()
    if prov.candidate_sha is None or problems:
        supervisor.record(
            AutonomyDecision(
                Condition.EVIDENCE_NOT_BOUND_TO_CANDIDATE,
                "delivery/v1",
                Action.REVOKE_EVIDENCE_REQUIRE_REVALIDATION,
                task_id,
                {"problems": "; ".join(problems)[:300] or "no candidate"},
                {"candidate": prov.candidate_sha or "none"},
            )
        )
        return finish("NOT_PUBLISHED", "evidence does not authorize delivering this candidate; nothing was published")
    sha = prov.candidate_sha

    base_sha, early = _sync_base(supervisor, report, remote, base)
    if early is not None:
        return finish(*early)

    branch = branch_name(task_id, sha)
    report.branch = branch
    push = facts.git.run("push", "--quiet", remote, f"{sha}:refs/heads/{branch}", check=False)  # never --force: an existing branch must already be this SHA
    if push.returncode != 0 and _PUSH_DENIED.search(push.stderr):
        raise PushDenied(push.stderr.strip()[:300])
    if push.returncode != 0:
        supervisor.record(
            AutonomyDecision(
                Condition.EXTERNAL_WORKSPACE_MUTATION,
                "delivery/v1",
                Action.FAIL_CLOSED_QUARANTINE,
                task_id,
                {"mutation": "REMOTE_BRANCH_DIFFERS", "branch": branch},
                {"candidate": sha},
                {"push_refused": push.stderr.strip()[:300], "force_push": False},
            )
        )
        return finish("NOT_PUBLISHED", f"{branch} already exists on {remote} at a different commit; it was not overwritten")

    evidence = evidence_summary(supervisor, task_id, sha)
    report.evidence = evidence
    body = pr_body(task_id, title, sha, evidence)
    pr = pulls.find_open(branch)
    if pr is not None and pr.base_ref != base:  # a reused PR's base is verified, never assumed
        pr = pulls.set_base(pr.number, base)
    pr = pulls.edit(pr.number, title, body) if pr is not None else pulls.open_pr(branch, base, title, body, sha)
    report.pr_number = pr.number

    deadline = clock() + wait_seconds
    run = ci.run_for(sha)
    while (run is None or not run.complete) and clock() < deadline:
        sleep(poll_seconds)
        run = ci.run_for(sha)
    if run is None or not run.complete:
        return finish("PUBLISHED_WAITING", f"hosted CI for {sha[:7]} did not finish within {int(wait_seconds)}s; delivery will be re-evaluated")

    # The base may have moved while CI ran: look again right before judging, so a stale view can never become MERGE_READY.
    base_sha, early = _sync_base(supervisor, report, remote, base)
    if early is not None:
        return finish(*early)
    assert base_sha is not None
    decision = supervisor.assess_ci(task_id, sha, base_sha, scope=supervisor._scope(task_id, sha))
    diagnosis = supervisor.last_ci_diagnosis
    assert diagnosis is not None
    report.ci = diagnosis.to_dict()
    if decision.action in REMEDIATION_ACTIONS:
        supervisor.remediate_from_ci(task_id, sha, diagnosis, decision)
        return finish("PUBLISHED_REMEDIATING", "hosted CI shows a failure this candidate introduced; the task was sent back for remediation")

    current = pulls.get(pr.number) or pr
    if current.head_sha and current.head_sha != sha:
        # someone pushed to the PR branch after delivery: the CI and evidence above belong to a different commit
        supervisor.record(supervisor._pr_head_moved(task_id, pr.number, sha, current.head_sha))
        return finish("PUBLISHED_NOT_READY", f"PR #{pr.number} head is {current.head_sha[:7]}, not the validated candidate {sha[:7]}; nothing was merged")
    mergeable_deadline = clock() + wait_seconds
    while current.mergeable is None and clock() < mergeable_deadline:  # hosts compute mergeability lazily
        sleep(poll_seconds)
        current = pulls.get(pr.number) or current
    verdict = supervisor.evaluate_merge(task_id, ci=diagnosis, mergeable=current.mergeable)
    report.unsatisfied = [f"{c.name}: {c.reason}" for c in verdict.unsatisfied]
    if not verdict.may_merge:
        return finish("PUBLISHED_NOT_READY", "merge policy not satisfied: " + "; ".join(report.unsatisfied))
    if merge:
        outcome = supervisor.merge_when_ready(task_id, pr.number, ci=diagnosis, remote=remote)
        report.merge_performed = outcome.action is Action.MERGE
        return finish("MERGED" if report.merge_performed else "PUBLISHED_NOT_READY", f"merge attempted: {outcome.trace_line()}")
    return finish("MERGE_READY", "every merge-policy condition holds; automatic merge is disabled, so the supervisor recommends merging PR "
                  f"#{pr.number} at head {sha[:7]}")


def write_report(project: Path, report: DeliveryReport) -> Path:
    directory = Path(project) / ".stagemesh" / "autonomy" / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", report.task_id)
    (directory / f"{stem}.json").write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    path = directory / f"{stem}.md"
    path.write_text(report.to_markdown(), encoding="utf-8")
    return path

