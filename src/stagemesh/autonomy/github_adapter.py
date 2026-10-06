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

    def merge(self, number: int, expected_head_sha: str, method: str = "squash") -> MergeOutcome:
        """Merge only if the PR head is still `expected_head_sha` (the SHA that was validated and reviewed)."""
        if method not in {"merge", "squash", "rebase"}:
            raise ValueError(f"unsupported merge method {method!r}")
        status, payload = self._request("PUT", f"/pulls/{int(number)}/merge", {"sha": expected_head_sha, "merge_method": method})
        message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
        if status == 200 and isinstance(payload, dict) and payload.get("merged"):
            return MergeOutcome(True, str(payload.get("sha") or "") or None)
        if status == 409:
            return MergeOutcome(False, None, "head_sha_mismatch")  # the head moved after validation: someone else wrote to the PR branch
        if status == 405:
            return MergeOutcome(False, None, f"not_mergeable: {message}")
        return MergeOutcome(False, None, f"{status}: {message}")

    def _to_pull_request(self, data: dict) -> PullRequest:
        head, base = data.get("head") or {}, data.get("base") or {}
        if data.get("merged"):
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


class GitHubHostedCI(GitHubClientBase):
    """`HostedCI` over check runs. Check-run payloads carry no logs, so failure identity is gate-level unless output text lists tests."""

    def run_for(self, sha: str) -> HostedCIRun | None:
        status, payload = self._request("GET", f"/commits/{quote(sha)}/check-runs?per_page=100", missing_ok=True)
        if status == 404:
            return None
        if status != 200 or not isinstance(payload, dict):
            raise GitHubAdapterError(status, f"unexpected check-runs response for {sha}")
        runs = payload.get("check_runs") or []
        gates: dict[str, GateOutcome] = {}
        complete = True
        for run in runs:
            outcome = self._gate(run)
            complete = complete and outcome.conclusion is not Conclusion.PENDING
            gates[outcome.name] = outcome
        return HostedCIRun(sha, gates, complete=complete and bool(runs))

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
        return GateOutcome(name, conclusion, text, tuple(dict.fromkeys(_FAILED_TEST.findall(text))))
