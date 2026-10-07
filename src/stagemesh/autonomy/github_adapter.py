"""GitHub connector boundary for the supervisor: pull requests, hosted CI (check runs), base retargeting and head-pinned merges.

Implements the `PullRequestAdapter` and `HostedCI` protocols on top of the existing transport-injected `GitHubTransport`, so every
behavior is testable with a recorded/fake transport and the same code runs live with `UrlLibGitHubTransport`.

Two safety properties live here:
* a merge always names the exact head SHA that was validated and reviewed (`sha` in the merge request); if the PR head moved the host
  refuses with 409 and the adapter reports `head_sha_mismatch`, which the supervisor treats as an external mutation;
* authorization problems (401/403 without a rate limit) are raised as `GitHubAuthorizationError`, which maps to the typed
  `EXTERNAL_SYSTEM_AUTHORIZATION_REQUIRED` escalation; rate limits are a wait, never an escalation.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from ..github import GitHubTransport, parse_retry_after
from .ci_diagnosis import Conclusion, GateOutcome, HostedCIRun
from .decisions import Escalation, EscalationReason
from .dependencies import CIRollup, MergeOutcome, PRState, PullRequest

_FAILED_TEST = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_CONCLUSIONS = {
    "success": Conclusion.SUCCESS,
    "failure": Conclusion.FAILURE,
    "cancelled": Conclusion.CANCELLED,
    "timed_out": Conclusion.TIMED_OUT,
    "skipped": Conclusion.SKIPPED,
    "neutral": Conclusion.SKIPPED,
    "stale": Conclusion.CANCELLED,
    "startup_failure": Conclusion.FAILURE,
    "action_required": Conclusion.FAILURE,
}


class GitHubAdapterError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


class GitHubAuthorizationError(GitHubAdapterError):
    """401, or 403 that is not a rate limit: a human must grant access."""


class GitHubRateLimited(GitHubAdapterError):
    def __init__(self, status: int, message: str, retry_after: float):
        super().__init__(status, message)
        self.retry_after = retry_after


def authorization_escalation(error: GitHubAuthorizationError, needed: str) -> Escalation:
    return Escalation(
        EscalationReason.EXTERNAL_SYSTEM_AUTHORIZATION_REQUIRED,
        attempted=("used the configured GitHub credentials", f"retried after distinguishing {error.status} from a rate limit"),
        why_undeterminable=f"GitHub refused the request ({error.status}); StageMesh cannot grant itself access",
        smallest_decision=f"Grant the StageMesh GitHub token {needed}, or confirm it should stay without that access?",
    )


class GitHubClientBase:
    def __init__(self, owner: str, repo: str, transport: GitHubTransport):
        self.owner, self.repo, self.transport = owner, repo, transport

    @property
    def _root(self) -> str:
        return f"/repos/{quote(self.owner)}/{quote(self.repo)}"

    def _request(self, method: str, path: str, body: dict[str, object] | None = None, *, missing_ok: bool = False) -> tuple[int, object]:
        status, headers, payload = self.transport.request(method, f"{self._root}{path}", body)
        lowered = {k.casefold(): v for k, v in headers.items()}
        message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
        if status == 404 and missing_ok:
            return status, payload
        if status in {403, 429} and (lowered.get("x-ratelimit-remaining") == "0" or status == 429 or "rate limit" in message.casefold()):
            raise GitHubRateLimited(status, message or "rate limited", parse_retry_after(lowered.get("retry-after")))
        if status in {401, 403}:
            raise GitHubAuthorizationError(status, message or "not authorized")
        return status, payload


class GitHubPullRequests(GitHubClientBase):
    """`PullRequestAdapter` over the GitHub REST API, plus head-pinned merge."""

    def __init__(self, owner: str, repo: str, transport: GitHubTransport, ci: GitHubHostedCI | None = None):
        super().__init__(owner, repo, transport)
        self.ci = ci or GitHubHostedCI(owner, repo, transport)

    def get(self, number: int) -> PullRequest | None:
        status, payload = self._request("GET", f"/pulls/{int(number)}", missing_ok=True)
        if status == 404:
            return None
        if status != 200 or not isinstance(payload, dict):
            raise GitHubAdapterError(status, f"unexpected pull request response for #{number}")
        return self._to_pull_request(payload)

    def set_base(self, number: int, base_ref: str) -> PullRequest:
        status, payload = self._request("PATCH", f"/pulls/{int(number)}", {"base": base_ref})
        if status != 200 or not isinstance(payload, dict):
            raise GitHubAdapterError(status, f"could not retarget #{number} to {base_ref}")
        return self._to_pull_request(payload)

    def find_by_head(self, head_ref: str) -> PullRequest | None:
        status, payload = self._request("GET", f"/pulls?state=all&head={quote(self.owner)}:{quote(head_ref, safe='')}&sort=created&direction=desc&per_page=5")
        if status != 200 or not isinstance(payload, list):
            raise GitHubAdapterError(status, f"unexpected pull request listing for {head_ref}")
        return self._detailed(payload[0]) if payload else None

    def find_open(self, head_ref: str) -> PullRequest | None:
        """The open PR whose head is `head_ref` (a branch of this repository), if any."""
        status, payload = self._request("GET", f"/pulls?state=open&head={quote(self.owner)}:{quote(head_ref, safe='')}&per_page=5")
        if status != 200 or not isinstance(payload, list):
            raise GitHubAdapterError(status, f"unexpected pull request listing for {head_ref}")
        return self._detailed(payload[0]) if payload else None

    def _detailed(self, listed: dict) -> PullRequest:
        """List payloads omit mergeability and the merged flag: read the single-PR resource for the facts policy depends on."""
        full = self.get(int(listed["number"]))
        return full if full is not None else self._to_pull_request(listed)

    def open_pr(self, head_ref: str, base_ref: str, title: str, body: str, head_sha: str | None = None) -> PullRequest:
        status, payload = self._request("POST", "/pulls", {"title": title, "head": head_ref, "base": base_ref, "body": body})
        if status != 201 or not isinstance(payload, dict):
            message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
            raise GitHubAdapterError(status, f"could not open a pull request for {head_ref}: {message}")
        return self._to_pull_request(payload)

    def edit(self, number: int, title: str, body: str) -> PullRequest:
        status, payload = self._request("PATCH", f"/pulls/{int(number)}", {"title": title, "body": body})
        if status != 200 or not isinstance(payload, dict):
            raise GitHubAdapterError(status, f"could not update #{number}")
        return self._to_pull_request(payload)

    def merge(self, number: int, expected_head_sha: str, method: str = "squash") -> MergeOutcome:
        """Merge only if the PR head is still `expected_head_sha` (the SHA that was validated and reviewed)."""
        if method not in {"merge", "squash", "rebase"}:
            raise ValueError(f"unsupported merge method {method!r}")
        status, payload = self._request("PUT", f"/pulls/{int(number)}/merge", {"sha": expected_head_sha, "merge_method": method})
        message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
        if status == 200 and isinstance(payload, dict) and payload.get("merged"):
            return MergeOutcome(True, str(payload.get("sha") or "") or None)
        if status == 409:
            lowered = message.casefold()
            if "base branch was modified" in lowered:
                return MergeOutcome(False, None, "base_modified")  # the base moved, not the head: the next pass refreshes against it
            if "head branch was modified" in lowered:
                return MergeOutcome(False, None, "head_sha_mismatch")  # the head moved after validation: someone else wrote to the PR branch
            return MergeOutcome(False, None, f"409: {message}")
        if status == 405:
            return MergeOutcome(False, None, f"not_mergeable: {message}")
        return MergeOutcome(False, None, f"{status}: {message}")

    def _to_pull_request(self, data: dict) -> PullRequest:
        head, base = data.get("head") or {}, data.get("base") or {}
        if data.get("merged") or data.get("merged_at"):  # list payloads carry merged_at, not merged
            state = PRState.MERGED
        elif data.get("state") == "closed":
            state = PRState.CLOSED
        else:
            state = PRState.OPEN
        mergeable = data.get("mergeable")
        if data.get("mergeable_state") == "dirty":
            mergeable = False
        head_sha = str(head.get("sha") or "")
        return PullRequest(
            number=int(data["number"]),
            head_sha=head_sha,
            head_ref=str(head.get("ref") or ""),
            base_ref=str(base.get("ref") or ""),
            state=state,
            ci=self.ci.rollup(head_sha) if head_sha and state is PRState.OPEN else CIRollup.SUCCESS,
            mergeable=mergeable if isinstance(mergeable, bool) else None,
            merge_commit_sha=str(data["merge_commit_sha"]) if data.get("merge_commit_sha") and state is PRState.MERGED else None,
        )


def _worst_gate(outcomes: list[GateOutcome], severity: dict[Conclusion, int]) -> GateOutcome:
    """Keep the worst conclusion, and every failure detail that shares that rank.

    Two suites can both fail under the same check name. Picking one log would let a known red suite hide a new test.
    """
    worst = max(severity.get(outcome.conclusion, 0) for outcome in outcomes)
    tied = [outcome for outcome in outcomes if severity.get(outcome.conclusion, 0) == worst]
    if len(tied) == 1:
        return tied[0]
    tests: list[str] = []
    for outcome in tied:
        for test in outcome.failing_tests:
            if test not in tests:
                tests.append(test)
    log = "\n".join(outcome.log for outcome in tied if outcome.log)
    earlier = tuple(conclusion for outcome in tied for conclusion in outcome.rerun_conclusions)
    conclusion = Conclusion.FAILURE if any(outcome.conclusion is Conclusion.FAILURE for outcome in tied) else tied[0].conclusion
    return GateOutcome(tied[0].name, conclusion, log, tuple(tests), earlier, tied[0].ref)


class GitHubHostedCI(GitHubClientBase):
    """`HostedCI` over check runs. Check-run payloads carry no logs, so failure identity is gate-level unless output text lists tests."""

    def run_for(self, sha: str) -> HostedCIRun | None:
        runs: list[dict] = []
        for page in range(1, 11):  # a gate beyond the first page must not silently disappear
            status, payload = self._request("GET", f"/commits/{quote(sha)}/check-runs?per_page=100&page={page}", missing_ok=True)
            if status == 404 and page == 1:
                return None
            if status != 200 or not isinstance(payload, dict):
                raise GitHubAdapterError(status, f"unexpected check-runs response for {sha}")
            batch = payload.get("check_runs") or []
            runs.extend(batch)
            total = payload.get("total_count")
            # A missing total_count must not end pagination: a full page can hide a later failure.
            if len(batch) < 100 or (isinstance(total, int) and not isinstance(total, bool) and len(runs) >= total):
                break
        # Re-runs of a check are further check runs of the SAME check suite and name: within a suite the newest decides and earlier ones
        # are attempts. Same-named checks from DIFFERENT suites (other workflows or apps) are independent gates: the worst verdict wins,
        # so a later green can never hide an earlier red.
        suites: dict[tuple[str, str], list[dict]] = {}
        for run in runs:
            suite = str((run.get("check_suite") or {}).get("id") or "")
            suites.setdefault((suite, str(run.get("name") or "unnamed")), []).append(run)
        per_name: dict[str, list[GateOutcome]] = {}
        for (_suite, name), attempts in suites.items():
            attempts.sort(key=lambda r: (str(r.get("started_at") or ""), int(r.get("id") or 0)), reverse=True)
            latest = self._gate(attempts[0])
            earlier = tuple(self._gate(r).conclusion for r in attempts[1:] if r.get("status") == "completed")
            per_name.setdefault(name, []).append(GateOutcome(latest.name, latest.conclusion, latest.log, latest.failing_tests, earlier, latest.ref))
        severity = {Conclusion.PENDING: 3, Conclusion.FAILURE: 2, Conclusion.TIMED_OUT: 2, Conclusion.CANCELLED: 2}
        gates: dict[str, GateOutcome] = {}
        complete = True
        for name, outcomes in per_name.items():
            outcome = _worst_gate(outcomes, severity)
            complete = complete and not any(o.conclusion is Conclusion.PENDING for o in outcomes)
            gates[name] = outcome
        return HostedCIRun(sha, gates, complete=complete and bool(runs), environment="github-actions")

    def rerun(self, sha: str, gates) -> bool:
        """Ask the host to rerun the failed jobs behind these gates (an Actions job id is its check run id). False if any request fails."""
        requested = False
        for gate in gates:
            if gate.ref is None:
                return False
            status, _ = self._request("POST", f"/actions/jobs/{quote(gate.ref)}/rerun")
            if status not in {201, 200}:
                return False
            requested = True
        return requested

    def rollup(self, sha: str) -> CIRollup:
        run = self.run_for(sha)
        if run is None or not run.gates:
            return CIRollup.PENDING
        if any(g.conclusion.failed for g in run.gates.values()):
            return CIRollup.FAILURE
        return CIRollup.SUCCESS if run.complete else CIRollup.PENDING

    @staticmethod
    def _gate(run: dict) -> GateOutcome:
        name = str(run.get("name") or "unnamed")
        if run.get("status") != "completed":
            return GateOutcome(name, Conclusion.PENDING)
        output = run.get("output") or {}
        text = "\n".join(str(output.get(key) or "") for key in ("title", "summary", "text") if output.get(key))
        conclusion = _CONCLUSIONS.get(str(run.get("conclusion")), Conclusion.FAILURE)
        ref = str(run["id"]) if run.get("id") is not None else None
        return GateOutcome(name, conclusion, text, tuple(dict.fromkeys(_FAILED_TEST.findall(text))), ref=ref)
