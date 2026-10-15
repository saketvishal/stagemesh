"""Opt-in exact-candidate fast-forward landing using the existing supervisor policy."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from .autonomy.ci_diagnosis import Conclusion
from .autonomy.decisions import Action
from .autonomy.dependencies import MergeOutcome, PRState
from .autonomy.github_adapter import GitHubHostedCI, GitHubPullRequests
from .autonomy.merge_policy import IntegrationPolicy
from .autonomy.supervisor import Supervisor
from .concurrency import IntegrationLock
from .git import GitWorkspace
from .queue_control import QueueStopControl
from .survivor_wave import is_task_pr_branch


def checks_ready(ci, sha: str, required: tuple[str, ...]) -> bool:
    run = ci.run_for(sha)
    return bool(run and run.sha == sha and run.complete and required and set(required) <= set(run.gates)
                and all(g.conclusion is Conclusion.SUCCESS for g in run.gates.values()))


class FastForwardPullRequests(GitHubPullRequests):
    """Change only the landing operation: normal push of an ancestor-proven commit.

    The supervisor still owns evidence, independent review, dependencies, freshness,
    findings, CI diagnosis and post-merge verification. No squash/rebase/force push.
    """

    def configure_ff(self, project, store, branch, required_checks, ci):
        self.project, self.store = Path(project), store
        self.branch, self.required_checks, self.ci = branch, required_checks, ci
        self.expected_base = None
        self.deadline = float("inf")
        self.stop_control = QueueStopControl(store)

    def bounded_git(self, *args, after_landing=False, check=True):
        remaining = 20 if after_landing else min(20, self.deadline - time.time())
        if remaining <= 0:
            raise RuntimeError("merge deadline reached")
        result = subprocess.run(["git", *args], cwd=self.project, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=remaining, check=False)
        if check and result.returncode:
            raise RuntimeError("git operation refused: " + result.stderr[-200:])
        return result

    def remote_tip(self, ref: str, *, after_landing=False) -> str | None:
        output = self.bounded_git("ls-remote", "origin", ref, after_landing=after_landing).stdout.split()
        return output[0] if output else None

    def sync_local(self, candidate):
        with IntegrationLock(self.project / ".stagemesh/integration.lock", timeout_seconds=10).hold("ff-sync"):
            git = lambda *args: self.bounded_git(*args, after_landing=True)
            if git("symbolic-ref", "HEAD").stdout.strip() != f"refs/heads/{self.branch}":
                return "remote merged; local checkout changed"
            if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
                return "remote merged; local tracked workspace changed"
            if self.remote_tip(f"refs/heads/{self.branch}", after_landing=True) != candidate:
                return "remote integration target advanced after landing"
            local = git("rev-parse", "HEAD").stdout.strip()
            expected = self.expected_base
            if expected == candidate and local != candidate:
                intent = self.store.conn.execute(
                    "SELECT payload FROM audit_events WHERE event_type=? AND json_extract(payload,'$.candidate_sha')=? ORDER BY created_at DESC LIMIT 1",
                    ("window.fast_forward_push_intent", candidate)).fetchone()
                expected = json.loads(intent["payload"])["base_sha"] if intent else None
            if local not in {candidate, expected}:
                return "local integration target moved outside the verified landing"
            git("merge", "--ff-only", candidate)
            if git("rev-parse", "HEAD").stdout.strip() != candidate:
                return "local integration target is not the exact candidate"
            if self.remote_tip(f"refs/heads/{self.branch}", after_landing=True) != candidate:
                return "remote integration target changed during synchronization"
            return None

    def merge(self, number: int, expected_head_sha: str, method: str = "ff-only") -> MergeOutcome:
        if method != "ff-only":
            raise ValueError("this adapter accepts only ff-only")
        if time.time() >= self.deadline:
            return MergeOutcome(False, reason="merge_deadline_reached")
        with IntegrationLock(self.project / ".stagemesh/integration.lock", timeout_seconds=10).hold("ff-merge"):
            git = GitWorkspace(self.project)
            pr = self.get(number)
            if not pr or pr.state is not PRState.OPEN or pr.head_sha != expected_head_sha:
                return MergeOutcome(False, reason="head_sha_mismatch")
            if pr.base_ref != self.branch or pr.mergeable is not True:
                return MergeOutcome(False, reason="base_or_mergeability_changed")
            if self.stop_control.blocks_admission() or list(self.store.running_executions()) or any(self.store.has_active_claim(row["id"]) for row in self.store.tasks()):
                return MergeOutcome(False, reason="queue_paused_or_execution_active")
            if git.run("symbolic-ref", "HEAD").stdout.strip() != f"refs/heads/{self.branch}":
                return MergeOutcome(False, reason="checkout_changed")
            if git.run("status", "--porcelain", "--untracked-files=no").stdout.strip():
                return MergeOutcome(False, reason="tracked_workspace_dirty")
            base_ref = f"refs/heads/{self.branch}"
            local = git.run("rev-parse", base_ref).stdout.strip()
            if not self.expected_base or local != self.expected_base or self.remote_tip(base_ref) != self.expected_base:
                return MergeOutcome(False, reason="base_modified")
            if git.run("merge-base", "--is-ancestor", self.expected_base, expected_head_sha, check=False).returncode:
                return MergeOutcome(False, reason="not_fast_forward")
            if self.remote_tip(f"refs/heads/{pr.head_ref}") != expected_head_sha:
                return MergeOutcome(False, reason="head_sha_mismatch")
            if not checks_ready(self.ci, expected_head_sha, self.required_checks):
                return MergeOutcome(False, reason="hosted_checks_not_green")
            # Recheck after hosted calls: the operator or another writer may have moved
            # the queue, checkout, PR head or base while CI was being observed.
            if time.time() >= self.deadline:
                return MergeOutcome(False, reason="merge_deadline_reached")
            if self.stop_control.blocks_admission():
                return MergeOutcome(False, reason="queue_paused")
            if git.run("symbolic-ref", "HEAD").stdout.strip() != f"refs/heads/{self.branch}" or git.run("status", "--porcelain", "--untracked-files=no").stdout.strip():
                return MergeOutcome(False, reason="checkout_changed")
            if self.remote_tip(base_ref) != self.expected_base:
                return MergeOutcome(False, reason="base_modified")
            if self.remote_tip(f"refs/heads/{pr.head_ref}") != expected_head_sha:
                return MergeOutcome(False, reason="head_sha_mismatch")
            if self.stop_control.blocks_admission() or list(self.store.running_executions()) or any(self.store.has_active_claim(row["id"]) for row in self.store.tasks()):
                return MergeOutcome(False, reason="queue_paused_or_execution_active")
            if git.run("symbolic-ref", "HEAD").stdout.strip() != f"refs/heads/{self.branch}" or git.run("rev-parse", "HEAD").stdout.strip() != self.expected_base or git.run("status", "--porcelain", "--untracked-files=no").stdout.strip():
                return MergeOutcome(False, reason="checkout_changed")
            self.store.add_audit_event("window.fast_forward_push_intent", {
                "pr": number, "candidate_sha": expected_head_sha, "base_sha": self.expected_base})
            pushed = self.bounded_git("push", "origin", f"{expected_head_sha}:{base_ref}", check=False)
            if pushed.returncode:
                return MergeOutcome(False, reason="fast_forward_push_refused: " + pushed.stderr[-200:])
            if self.remote_tip(base_ref, after_landing=True) != expected_head_sha:
                return MergeOutcome(False, reason="remote_landing_not_exact")
            self.bounded_git("fetch", "origin", f"refs/heads/{self.branch}:refs/remotes/origin/{self.branch}", after_landing=True)
            return MergeOutcome(True, expected_head_sha)


def make_merger(store, project: Path, base_ref: str, required_checks: tuple[str, ...]):
    from .autonomy.cli import _github_token
    from .github import UrlLibGitHubTransport, detect_github_repository

    repository = detect_github_repository(project, "origin")
    if repository is None:
        raise ValueError("fast-forward auto-merge requires an origin GitHub repository")
    transport = UrlLibGitHubTransport(_github_token())
    ci = GitHubHostedCI(repository.owner, repository.repo, transport)
    pulls = FastForwardPullRequests(repository.owner, repository.repo, transport, ci)
    branch = base_ref.removeprefix("refs/heads/")
    pulls.configure_ff(project, store, branch, required_checks, ci)
    supervisor = Supervisor(store, project, integration_ref=f"refs/remotes/origin/{branch}",
                            hosted_ci=ci, pull_requests=pulls,
                            integration_policy=IntegrationPolicy(allow_baseline_ci_failures=False))

    def merge_record(record, deadline):
        url = urlparse(record.pr_url or "")
        prefix = f"/{repository.owner}/{repository.repo}/pull/"
        if url.scheme != "https" or url.hostname != "github.com" or not url.path.startswith(prefix):
            return {"merged": False, "reason": "unexpected PR URL"}
        number_text = url.path[len(prefix):]
        if not number_text.isdigit():
            return {"merged": False, "reason": "invalid PR number"}
        number = int(number_text)
        pulls.deadline = deadline
        while time.time() < deadline:
            if pulls.stop_control.blocks_admission():
                return {"merged": False, "reason": "queue admission " + pulls.stop_control.state_label()}
            pr = pulls.get(number)
            if not pr or not is_task_pr_branch(pr.head_ref, record.task_id, record.integrated_candidate) or pr.head_sha != record.integrated_candidate:
                return {"merged": False, "reason": "PR head no longer matches the staged candidate"}
            if pr.state is PRState.CLOSED:
                return {"merged": False, "reason": "PR closed without merging"}
            run = ci.run_for(record.integrated_candidate)
            if run and any(g.conclusion.failed for g in run.gates.values()):
                return {"merged": False, "reason": "hosted CI failed; candidate not merged"}
            if pr.state is PRState.MERGED or checks_ready(ci, record.integrated_candidate, required_checks):
                fetched = pulls.bounded_git("fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}", check=False)
                if fetched.returncode:
                    return {"merged": False, "reason": "remote base could not be fetched"}
                pulls.expected_base = supervisor.facts.resolve(supervisor.integration_ref)
                if not supervisor.facts.is_ancestor(pulls.expected_base, record.integrated_candidate):
                    return {"merged": False, "reason": "base advanced; candidate needs revalidation"}
                supervisor.assess_ci(record.task_id, record.integrated_candidate, pulls.expected_base, reruns_left=0)
                decision = supervisor.merge_when_ready(record.task_id, number, ci=supervisor.last_ci_diagnosis, method="ff-only")
                if decision.action is not Action.MERGE or not decision.detail.get("verified"):
                    return {"merged": False, "reason": decision.trace_line()}
                sync_error = pulls.sync_local(record.integrated_candidate)
                if sync_error:
                    return {"merged": False, "reason": sync_error}
                store.add_audit_event("window.fast_forward_merged", {"task_id": record.task_id,
                    "pr": number, "candidate_sha": record.integrated_candidate, "required_checks": list(required_checks)})
                return {"merged": True, "pr": number, "candidate_sha": record.integrated_candidate}
            time.sleep(min(10, max(0, deadline - time.time())))
        return {"merged": False, "reason": "hosted CI deadline reached; PR retained for retry"}

    return merge_record
