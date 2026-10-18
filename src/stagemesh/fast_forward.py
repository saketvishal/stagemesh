"""Opt-in exact-candidate fast-forward landing using the existing supervisor policy."""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from .autonomy.ci_diagnosis import Conclusion
from .autonomy.decisions import Action
from .autonomy.dependencies import MergeOutcome, PRState
from .autonomy.github_adapter import GitHubHostedCI, GitHubPullRequests
from .autonomy.merge_policy import IntegrationPolicy
from .autonomy.review_policy import severity_blocks
from .autonomy.supervisor import Supervisor
from .concurrency import IntegrationLock
from .contracts import ContractError, bound_contract_from_record
from .git import GitWorkspace
from .queue_control import QueueStopControl
from .review import independent_review_verified
from .survivor_wave import is_task_pr_branch


def checks_ready(ci, sha: str, required: tuple[str, ...]) -> bool:
    run = ci.run_for(sha)
    return bool(run and run.sha == sha and run.complete and required and set(required) <= set(run.gates)
                and all(g.conclusion is Conclusion.SUCCESS for g in run.gates.values()))


def landing_evidence_problem(store, task_id, sha):
    """Only the latest exact-bound successful evidence can authorize a landing."""
    candidate = store.latest_candidate(task_id)
    record = store.contract_binding(task_id, sha)
    if candidate is None or candidate["sha"] != sha or record is None:
        return "candidate_or_contract_binding_changed"
    try:
        bound = bound_contract_from_record(dict(record))
        expected = bound.evidence_payload()
        for kind in ("VALIDATION", "REVIEW", "INTEGRATION"):
            row = store.conn.execute(
                "SELECT status,payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1", (task_id, sha, kind)).fetchone()
            if row is None or row["status"] != "PASSED":
                return "latest_integration_not_passed" if kind == "INTEGRATION" else "latest_validation_or_review_not_passed"
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in expected.items()):
                return "latest_evidence_not_bound_to_candidate_contract"
            if kind == "REVIEW" and not independent_review_verified(payload):
                return "latest_review_not_independent"
    except (ContractError, TypeError, ValueError):
        return "latest_evidence_or_binding_invalid"
    if any(severity_blocks(str(f["severity"])) for f in store.open_findings_for_candidate(task_id, sha)):
        return "unresolved_blocking_findings"
    return None


class FastForwardPullRequests(GitHubPullRequests):
    """Land an ancestor-proven exact candidate with an atomic compare-and-swap push.

    The supervisor still owns evidence, independent review, dependencies, freshness,
    findings, CI diagnosis and post-merge verification. No squash, rebase or history rewrite.
    """

    def configure_ff(self, project, store, branch, required_checks, ci, *, policy="hosted", check_app_id=None):
        if policy not in {"hosted", "stagemesh"}:
            raise ValueError("unknown landing policy")
        if policy == "stagemesh" and (not required_checks or type(check_app_id) is not int or check_app_id <= 0):
            raise ValueError("StageMesh-controlled landing requires named checks and a trusted check app")
        self.landing_policy, self.check_app_id = policy, check_app_id
        self.project, self.store = Path(project), store
        self.branch, self.required_checks, self.ci = branch, required_checks, ci
        self.expected_base = None
        self.deadline = float("inf")
        self.stop_control = QueueStopControl(store)
        self.task_id = None
        self.policy_snapshot = None
        self.remote_url = "origin"
        self.hosted_requirements = set()

    def bind_remote(self):
        from .github import parse_github_remote

        self.reject_url_rewrites()
        fetch_urls = self.bounded_git("remote", "get-url", "--all", "origin").stdout.splitlines()
        push_urls = self.bounded_git("remote", "get-url", "--push", "--all", "origin").stdout.splitlines()
        if len(fetch_urls) != 1 or len(push_urls) != 1:
            raise ValueError("fast-forward landing requires exactly one fetch and one push URL")
        for url in (fetch_urls[0], push_urls[0]):
            repository = parse_github_remote(url)
            if repository is None or (repository.owner.casefold(), repository.repo.casefold()) != (self.owner.casefold(), self.repo.casefold()):
                raise ValueError("fetch/push destination differs from the policy-verified GitHub repository")
        self.remote_url = push_urls[0]

    def reject_url_rewrites(self):
        rewrites = self.bounded_git("config", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$", check=False)
        if rewrites.returncode != 1:
            raise ValueError("fast-forward landing requires Git URL rewriting to be absent")

    def get(self, number):
        status, payload = self._request("GET", f"/pulls/{int(number)}", missing_ok=True)
        if status == 404:
            return None
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeError("pull request identity unavailable")
        if payload.get("number") != int(number) or payload.get("state") not in {"open", "closed"}:
            raise RuntimeError("pull request number or state unavailable")
        expected = f"{self.owner}/{self.repo}".casefold()
        for side in ("head", "base"):
            full_name = ((payload.get(side) or {}).get("repo") or {}).get("full_name", "")
            if not isinstance(full_name, str) or full_name.casefold() != expected:
                raise RuntimeError("fast-forward landing requires a same-repository PR")
            if not isinstance((payload.get(side) or {}).get("ref"), str) or not payload[side]["ref"].strip():
                raise RuntimeError("pull request head or base ref unavailable")
        if not re.fullmatch(r"[0-9a-f]{40}", str((payload.get("head") or {}).get("sha") or "")):
            raise RuntimeError("pull request full head SHA unavailable")
        return self._to_pull_request(payload)

    def close_superseded(self, number, expected_head, replacement_url):
        """Close only an observed old owned head; never rewrite its branch or commits."""
        old = self.get(number)
        if old is None or old.head_sha != expected_head or old.base_ref != self.branch:
            raise RuntimeError("superseded PR identity changed; not closing it")
        if old.state is PRState.MERGED:
            raise RuntimeError("superseded candidate already landed; reconcile before refresh")
        if old.state is PRState.CLOSED:
            return
        status, payload = self._request("PATCH", f"/pulls/{number}", {"state": "closed"})
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeError("could not close superseded PR; preserved for retry")
        observed = self.get(number)
        if observed is None or observed.state is not PRState.CLOSED or observed.head_sha != expected_head:
            raise RuntimeError("superseded PR closure could not be verified")
        self.store.add_audit_event("window.landing_pr_superseded", {
            "task_id": self.task_id, "pr": number, "candidate_sha": expected_head,
            "replacement_url": replacement_url,
        })

    def host_policy(self, head_ref=None):
        """Verify the explicitly selected landing policy; never fall back after a 403.

        Hosted mode requires enforced no-rewrite policy and immutable published heads.
        Controlled mode verifies repository access and pins locally required checks.
        Neither mode installs, changes or relaxes GitHub rules.
        """
        if self.landing_policy == "stagemesh":
            status, repository = self._request("GET", "")
            if (status != 200 or not isinstance(repository, dict)
                    or str(repository.get("full_name", "")).casefold() != f"{self.owner}/{self.repo}".casefold()
                    or repository.get("archived") is not False or repository.get("disabled") is not False
                    or (repository.get("permissions") or {}).get("push") is not True):
                return "controlled_repository_identity_or_write_access_unverified"
            self.hosted_requirements = {(name, self.check_app_id) for name in self.required_checks}
            self.policy_snapshot = json.dumps({"policy": "stagemesh", "repository": repository["full_name"].casefold(),
                "checks": sorted(self.required_checks), "check_app_id": self.check_app_id}, sort_keys=True)
            return None
        observed = []
        requirements = set()
        refs = [(self.branch, {"non_fast_forward"})]
        if head_ref:
            refs.append((head_ref, {"update", "deletion"}))
        for ref, required in refs:
            rules = []
            for page in range(1, 21):
                status, payload = self._request("GET", f"/rules/branches/{quote(ref, safe='')}?per_page=100&page={page}")
                if status != 200 or not isinstance(payload, list):
                    return "hosted_branch_policy_unavailable"
                rules.extend(payload)
                if len(payload) < 100:
                    break
            else:
                return "hosted_branch_policy_incomplete"
            protected = set()
            for rule in rules:
                if (not isinstance(rule, dict) or rule.get("ruleset_source_type") != "Repository"
                        or not isinstance(rule.get("ruleset_source"), str)
                        or rule["ruleset_source"].casefold() != f"{self.owner}/{self.repo}".casefold()):
                    return "unsupported_or_inherited_branch_policy"
                rule_id = rule.get("ruleset_id")
                if type(rule_id) is not int:
                    return "hosted_branch_policy_incomplete"
                status, policy = self._request("GET", f"/rulesets/{rule_id}")
                if (status != 200 or not isinstance(policy, dict)
                        or policy.get("id") != rule_id or policy.get("target") != "branch"
                        or policy.get("enforcement") != "active" or policy.get("bypass_actors") != []):
                    return "hosted_branch_policy_unknown_or_bypassable"
                observed.append((ref, rule, policy))
                kind = rule.get("type")
                if not any(item.get("type") == kind for item in policy.get("rules", []) if isinstance(item, dict)):
                    return "hosted_branch_policy_changed"
                if kind in {"pull_request", "merge_queue"} and ref == self.branch:
                    return "host_requires_PR_merge_use_PR_only_mode"
                if kind == "update" and (rule.get("parameters") or {}).get("update_allows_fetch_and_merge") is not False:
                    return "published_head_allows_other_updates"
                protected.add(kind)
                if ref == self.branch and kind == "required_status_checks":
                    for check in (rule.get("parameters") or {}).get("required_status_checks", []):
                        requirements.add((check["context"], check.get("integration_id")))
            if not required <= protected:
                return "host_requires_no_rewrite_and_immutable_PR_heads"
        status, classic = self._request("GET", f"/branches/{quote(self.branch, safe='')}/protection", missing_ok=True)
        if status != 404:
            if status != 200 or not isinstance(classic, dict) or not (classic.get("enforce_admins") or {}).get("enabled"):
                return "classic_branch_policy_unknown_or_bypassable"
            if classic.get("required_pull_request_reviews"):
                return "host_requires_PR_merge_use_PR_only_mode"
            checks = classic.get("required_status_checks") or {}
            requirements.update((check["context"], check.get("app_id")) for check in checks.get("checks", []))
            requirements.update((context, None) for context in checks.get("contexts", []))
        observed.append(classic if status != 404 else None)
        self.policy_snapshot = json.dumps(observed, sort_keys=True)
        self.hosted_requirements = requirements
        return None

    def required_statuses_ready(self, sha):
        if not self.hosted_requirements:
            return True
        observations = {}
        for resource, key in (("statuses", None), ("check-runs", "check_runs")):
            values = []
            for page in range(1, 21):
                status, payload = self._request("GET", f"/commits/{sha}/{resource}?per_page=100&page={page}")
                batch = payload.get(key) if key and isinstance(payload, dict) else payload
                if status != 200 or not isinstance(batch, list):
                    return False
                values.extend(batch)
                if len(batch) < 100:
                    break
            else:
                return False
            observations[resource] = values
        for context, app_id in self.hosted_requirements:
            statuses = [r for r in observations["statuses"] if r.get("context") == context]
            runs = [r for r in observations["check-runs"] if r.get("name") == context
                and r.get("head_sha") == sha and (app_id is None or (r.get("app") or {}).get("id") == app_id)]
            if not runs and (app_id is not None or not statuses):
                return False
            if statuses and max(statuses, key=lambda r: int(r.get("id", 0))).get("state") != "success":
                return False
            suites = {}
            for run in runs:
                key = ((run.get("check_suite") or {}).get("id"), (run.get("app") or {}).get("id"))
                if int(run.get("id", 0)) > int(suites.get(key, {}).get("id", -1)):
                    suites[key] = run
            if any(run.get("status") != "completed" or run.get("conclusion") != "success" for run in suites.values()):
                return False
        return True

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
        output = self.bounded_git("ls-remote", self.remote_url, ref, after_landing=after_landing).stdout.split()
        return output[0] if output else None

    def historical_ancestor(self, candidate: str, tip: str) -> bool:
        """Observe completed history without fetching into the client checkout."""
        if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (candidate, tip)):
            return False
        local = GitWorkspace(self.project).run("merge-base", "--is-ancestor", candidate, tip, check=False)
        if local.returncode == 0:
            return True
        if local.returncode == 1:
            return False
        # A newly observed remote tip may not exist locally yet. This proof only
        # retires completed history; it never authorizes a new landing.
        status, data = self._request("GET", f"/compare/{candidate}...{tip}?per_page=1")
        if status != 200 or not isinstance(data, dict):
            return False
        base, merge_base = data.get("base_commit"), data.get("merge_base_commit")
        return bool(data.get("status") in {"ahead", "identical"}
            and isinstance(base, dict) and base.get("sha") == candidate
            and isinstance(merge_base, dict) and merge_base.get("sha") == candidate)

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
            policy_error = self.host_policy(pr.head_ref)
            if policy_error:
                return MergeOutcome(False, reason=policy_error)
            policy_snapshot = self.policy_snapshot
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
            if not self.required_statuses_ready(expected_head_sha):
                return MergeOutcome(False, reason="server_required_checks_not_green")
            policy_error = self.host_policy(pr.head_ref)
            if policy_error or self.policy_snapshot != policy_snapshot:
                return MergeOutcome(False, reason=policy_error or "hosted_branch_policy_changed")
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
            if self.task_id is not None:
                problem = landing_evidence_problem(self.store, self.task_id, expected_head_sha)
                if problem:
                    return MergeOutcome(False, reason=problem)
            self.store.add_audit_event("window.fast_forward_push_intent", {
                "repository": f"{self.owner}/{self.repo}", "base_ref": base_ref,
                "task_id": self.task_id,
                "pr": number, "candidate_sha": expected_head_sha, "base_sha": self.expected_base, "merge_policy": self.landing_policy})
            if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (self.expected_base, expected_head_sha)):
                return MergeOutcome(False, reason="literal_commit_ids_required")
            self.reject_url_rewrites()
            # The literal lease is compare-and-swap, not permission to rewrite:
            # ancestry is proved above; hosted mode additionally requires server no-rewrite policy.
            # An intermediate ancestor pushed by another writer must refuse too.
            pushed = self.bounded_git("push", "--atomic",
                f"--force-with-lease={base_ref}:{self.expected_base}",
                self.remote_url, f"{expected_head_sha}:{base_ref}", check=False)
            if pushed.returncode:
                return MergeOutcome(False, reason="fast_forward_push_refused: " + pushed.stderr[-200:])
            if self.remote_tip(base_ref, after_landing=True) != expected_head_sha:
                return MergeOutcome(False, reason="remote_landing_not_exact")
            self.store.add_audit_event("window.fast_forward_remote_landed", {
                "repository": f"{self.owner}/{self.repo}", "base_ref": base_ref,
                "task_id": self.task_id,
                "pr": number, "candidate_sha": expected_head_sha, "base_sha": self.expected_base, "merge_policy": self.landing_policy})
            # A fetch failure is a partial success, not permission to repeat a push.
            # The caller reports the journaled remote landing separately.
            self.bounded_git("fetch", self.remote_url, f"refs/heads/{self.branch}:refs/remotes/origin/{self.branch}", after_landing=True)
            return MergeOutcome(True, expected_head_sha)


def make_merger(store, project: Path, base_ref: str, required_checks: tuple[str, ...], *, policy="hosted", check_app_id=None):
    from .autonomy.cli import _github_token
    from .github import UrlLibGitHubTransport, detect_github_repository

    repository = detect_github_repository(project, "origin")
    if repository is None:
        raise ValueError("fast-forward auto-merge requires an origin GitHub repository")
    transport = UrlLibGitHubTransport(_github_token())
    ci = GitHubHostedCI(repository.owner, repository.repo, transport)
    pulls = FastForwardPullRequests(repository.owner, repository.repo, transport, ci)
    branch = base_ref.removeprefix("refs/heads/")
    pulls.configure_ff(project, store, branch, required_checks, ci, policy=policy, check_app_id=check_app_id)
    pulls.bind_remote()
    supervisor = Supervisor(store, project, integration_ref=f"refs/remotes/origin/{branch}",
                            hosted_ci=ci, pull_requests=pulls,
                            integration_policy=IntegrationPolicy(allow_baseline_ci_failures=False, verify_exact_landing=True))

    def progress(record, state, detail, *, heartbeat=False):
        now = time.time()
        previous, at = notices.get(record.task_id, (None, 0.0))
        if previous == (state, detail) and (not heartbeat or now - at < 60):
            return
        notices[record.task_id] = ((state, detail), now)
        if previous != (state, detail):
            store.add_audit_event("window.landing_status", {
                "task_id": record.task_id, "candidate_sha": record.integrated_candidate,
                "pr_url": record.pr_url, "state": state, "reason": detail,
            })
        emit = getattr(merge_record, "progress", None)
        if callable(emit):
            emit(f"task {record.task_id}: landing {state} ({record.pr_url}): {detail}")

    notices = {}

    def validate_records(records, deadline):
        """Observe the entire stack before any member can move the integration ref."""
        for record in records:
            if time.time() >= deadline:
                return "deadline reached while verifying pending PRs"
            url = urlparse(record.pr_url or "")
            prefix = f"/{repository.owner}/{repository.repo}/pull/"
            if url.scheme != "https" or url.hostname != "github.com" or not url.path.startswith(prefix):
                return "unexpected PR URL"
            number = url.path[len(prefix):]
            if not number.isdigit():
                return "invalid PR number"
            task = store.get_task(record.task_id)
            if task is None or task["status"] != "DONE" or task["stage"] != "DONE":
                return "pending PR task is not complete"
            pr = pulls.get(int(number))
            if (pr is None or pr.number != int(number) or pr.base_ref != branch or pr.head_sha != record.integrated_candidate
                    or not re.fullmatch(r"[0-9a-f]{40}", pr.head_sha)
                    or not is_task_pr_branch(pr.head_ref, record.task_id, record.integrated_candidate)
                    or pr.state is PRState.CLOSED):
                return "pending PR observed head, base or ownership changed"
            if pr.state is PRState.MERGED and pr.merge_commit_sha != record.integrated_candidate:
                return "pending PR was not landed as the exact candidate"
            problem = landing_evidence_problem(store, record.task_id, record.integrated_candidate)
            if problem:
                return problem
        return None

    def attempt_record(record, deadline):
        url = urlparse(record.pr_url or "")
        prefix = f"/{repository.owner}/{repository.repo}/pull/"
        if url.scheme != "https" or url.hostname != "github.com" or not url.path.startswith(prefix):
            return {"merged": False, "reason": "unexpected PR URL"}
        number_text = url.path[len(prefix):]
        if not number_text.isdigit():
            return {"merged": False, "reason": "invalid PR number"}
        number = int(number_text)
        pulls.deadline = deadline
        pulls.task_id = record.task_id
        observed_base = None
        unknown_since = None
        missing_since = None
        while time.time() < deadline:
            if pulls.stop_control.blocks_admission():
                return {"merged": False, "reason": "queue admission " + pulls.stop_control.state_label()}
            pr = pulls.get(number)
            if not pr or not is_task_pr_branch(pr.head_ref, record.task_id, record.integrated_candidate) or pr.head_sha != record.integrated_candidate:
                return {"merged": False, "reason": "PR head no longer matches the staged candidate"}
            if pr.state is PRState.CLOSED:
                return {"merged": False, "reason": "PR closed without merging"}
            if pr.base_ref != branch:
                return {"merged": False, "reason": "PR base branch changed; automatic refresh refused"}
            policy_error = pulls.host_policy(pr.head_ref)
            if policy_error:
                return {"merged": False, "reason": policy_error}
            if pr.state is PRState.OPEN and (observed_base is None or pr.mergeable is False):
                fetched = pulls.bounded_git("fetch", pulls.remote_url, f"refs/heads/{branch}:refs/remotes/origin/{branch}", check=False)
                if fetched.returncode:
                    return {"merged": False, "reason": "remote base could not be fetched"}
                observed_base = supervisor.facts.resolve(supervisor.integration_ref)
                if not observed_base:
                    return {"merged": False, "reason": "remote base SHA unavailable"}
                if not supervisor.facts.is_ancestor(observed_base, record.integrated_candidate):
                    reason = "PR conflicts with current base" if pr.mergeable is False else "base advanced; candidate needs fresh validation and review"
                    progress(record, "refresh required", reason)
                    return {"merged": False, "refresh_required": True, "base_sha": observed_base,
                            "reason": reason}
            if pr.state is PRState.OPEN and pr.mergeable is None:
                # GitHub recomputes mergeability when the preceding stack member lands.
                unknown_since = time.time() if unknown_since is None else unknown_since
                progress(record, "waiting", "GitHub is computing mergeability", heartbeat=True)
                if time.time() - unknown_since >= 60:
                    return {"merged": False, "reason": "GitHub mergeability remained unknown for 60s; no merge attempted"}
                time.sleep(min(2, max(0, deadline - time.time())))
                continue
            if pr.state is PRState.OPEN and pr.mergeable is False:
                reason = "GitHub reports a conflict although the observed base is already ancestral; refusing an unsafe refresh"
                progress(record, "blocked", reason)
                return {"merged": False, "reason": reason}
            run = ci.run_for(record.integrated_candidate)
            if run and any(g.conclusion.failed for g in run.gates.values()):
                return {"merged": False, "reason": "hosted CI failed; candidate not merged"}
            missing = sorted(set(required_checks) - set(run.gates if run and run.sha == record.integrated_candidate else ()))
            if missing:
                missing_since = time.time() if missing_since is None else missing_since
                progress(record, "waiting", "hosted checks not reported: " + ", ".join(missing), heartbeat=True)
                if time.time() - missing_since >= 60:
                    return {"merged": False, "reason": "hosted checks missing after 60s: " + ", ".join(missing)}
            else:
                missing_since = None
            if checks_ready(ci, record.integrated_candidate, required_checks):
                if not pulls.required_statuses_ready(record.integrated_candidate):
                    return {"merged": False, "reason": "server-required checks are missing, pending or red"}
                fetched = pulls.bounded_git("fetch", pulls.remote_url, f"refs/heads/{branch}:refs/remotes/origin/{branch}", check=False)
                if fetched.returncode:
                    return {"merged": False, "reason": "remote base could not be fetched"}
                pulls.expected_base = supervisor.facts.resolve(supervisor.integration_ref)
                if not supervisor.facts.is_ancestor(pulls.expected_base, record.integrated_candidate):
                    return {"merged": False, "refresh_required": True, "base_sha": pulls.expected_base,
                            "reason": "base advanced; candidate needs revalidation"}
                problem = landing_evidence_problem(store, record.task_id, record.integrated_candidate)
                if problem:
                    return {"merged": False, "reason": problem}
                supervisor.assess_ci(record.task_id, record.integrated_candidate, pulls.expected_base, reruns_left=0)
                # The supervisor's already-merged recovery path is intentionally
                # permissive for ordinary observation. This opt-in workflow must
                # still recheck independence, findings and every named CI gate.
                dependencies = supervisor.assess_dependencies(record.task_id, number, apply=False)
                verdict = supervisor.evaluate_merge(record.task_id, ci=supervisor.last_ci_diagnosis,
                    dependencies=dependencies, mergeable=True if pr.state is PRState.MERGED else pr.mergeable)
                if not verdict.may_merge:
                    return {"merged": False, "reason": verdict.decision.trace_line()}
                decision = supervisor.merge_when_ready(record.task_id, number, ci=supervisor.last_ci_diagnosis, method="ff-only")
                if decision.action is not Action.MERGE or not decision.detail.get("verified"):
                    return {"merged": False, "reason": decision.trace_line()}
                problem = landing_evidence_problem(store, record.task_id, record.integrated_candidate)
                if problem:
                    return {"merged": False, "reason": "remote landed but evidence changed: " + problem}
                current_pr = pulls.get(number)
                if (not current_pr or current_pr.head_sha != record.integrated_candidate or current_pr.base_ref != branch
                        or current_pr.head_ref != pr.head_ref or current_pr.state is PRState.CLOSED):
                    return {"merged": False, "reason": "remote landed but PR identity changed; reconcile before continuing"}
                sync_error = pulls.sync_local(record.integrated_candidate)
                if sync_error:
                    return {"merged": False, "reason": sync_error}
                store.add_audit_event("window.fast_forward_merged", {"task_id": record.task_id,
                    "repository": f"{repository.owner}/{repository.repo}", "base_ref": base_ref,
                    "pr": number, "candidate_sha": record.integrated_candidate, "required_checks": list(required_checks), "merge_policy": policy})
                return {"merged": True, "remote_landed": True, "local_synced": True,
                    "pr": number, "candidate_sha": record.integrated_candidate}
            if not missing:
                progress(record, "waiting", "hosted checks pending for this exact candidate", heartbeat=True)
            time.sleep(min(10, max(0, deadline - time.time())))
        return {"merged": False, "reason": "hosted CI deadline reached; PR retained for retry"}

    def pending_records():
        from .survivor_wave import TaskRecord

        rows = store.conn.execute("SELECT event_type, payload FROM audit_events WHERE event_type IN (?, ?) ORDER BY created_at, rowid",
            ("window.fast_forward_push_intent", "window.fast_forward_merged"))
        rows = list(rows)
        completed_legacy = {}
        for row in rows:
            payload = json.loads(row["payload"])
            if row["event_type"].endswith("merged") and not payload.get("repository"):
                completed_legacy[(payload.get("pr"), payload.get("candidate_sha"))] = payload

        def verified_legacy_history(payload):
            sha, number = payload.get("candidate_sha"), payload.get("pr")
            completed = completed_legacy.get((number, sha))
            if not completed or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or type(number) is not int:
                return False
            pr = pulls.get(number)
            if (not pr or pr.state is not PRState.MERGED or pr.base_ref != branch or pr.head_sha != sha
                    or pr.merge_commit_sha != sha or not is_task_pr_branch(pr.head_ref, completed.get("task_id", ""), sha)):
                return False
            tip = pulls.remote_tip(base_ref)
            return bool(tip and pulls.historical_ancestor(sha, tip))

        pending = {}
        pending_policies = {}
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("repository") != f"{repository.owner}/{repository.repo}" or payload.get("base_ref") != base_ref:
                if row["event_type"].endswith("push_intent") and not payload.get("repository"):
                    if verified_legacy_history(payload):
                        continue
                    raise RuntimeError("legacy fast-forward intent lacks repository identity; reconcile it before continuing")
                continue
            key = (payload["task_id"], payload["pr"], payload["candidate_sha"])
            if row["event_type"].endswith("merged"):
                pending.pop(key, None)
                pending_policies.pop(key, None)
            else:
                pending_policies[key] = payload.get("merge_policy", "hosted")
                pending[key] = TaskRecord(payload["task_id"], outcome="done", ready_for_merge=True,
                    pr_url=f"https://github.com/{repository.owner}/{repository.repo}/pull/{payload['pr']}",
                    integrated_candidate=payload["candidate_sha"])
        if any(value != policy for value in pending_policies.values()):
            raise RuntimeError("pending landing uses a different merge policy; reconcile before changing policy")
        return list(pending.values())

    def merge_record(record, deadline):
        try:
            result = attempt_record(record, deadline)
        except Exception as exc:  # noqa: BLE001 -- preserve partial landing even if a connector fails
            result = {"merged": False, "reason": f"{type(exc).__name__}: {exc}"}
        if not result.get("merged"):
            number_text = urlparse(record.pr_url or "").path.rsplit("/", 1)[-1]
            if not number_text.isdigit():
                return result
            result.update(candidate_sha=record.integrated_candidate, pr=int(number_text))
            historical = store.conn.execute("SELECT 1 FROM audit_events WHERE event_type=? AND json_extract(payload,'$.task_id')=? AND json_extract(payload,'$.candidate_sha')=? AND json_extract(payload,'$.repository')=? AND json_extract(payload,'$.base_ref')=? LIMIT 1",
                ("window.fast_forward_remote_landed", record.task_id, record.integrated_candidate, f"{repository.owner}/{repository.repo}", base_ref)).fetchone()
            result["historical_remote_landed"] = bool(historical)
            try:
                pr = pulls.get(result["pr"])
                tip = pulls.remote_tip(f"refs/heads/{branch}", after_landing=True)
                attempted = any(r.integrated_candidate == record.integrated_candidate and r.task_id == record.task_id for r in pending_records())
                verified_pr = bool(pr and pr.state is PRState.MERGED and pr.base_ref == branch
                    and pr.head_sha == record.integrated_candidate and pr.merge_commit_sha == record.integrated_candidate)
                if tip == record.integrated_candidate or verified_pr:
                    result["remote_landed"] = True
                elif not tip or supervisor.facts.resolve(tip) is None:
                    result["remote_landed"] = None
                elif supervisor.facts.is_ancestor(record.integrated_candidate, tip):
                    result["remote_landed"] = True
                else:
                    result["remote_landed"] = None if attempted else False
            except Exception:  # noqa: BLE001 -- unknown must not be misreported as no remote change
                result["remote_landed"] = None
            if historical:
                result["remote_landed"] = True
            if result.get("remote_landed") is not False:
                result.pop("refresh_required", None)
            result["local_synced"] = False
            if result["remote_landed"] is not False:
                result["reason"] = "remote landing " + ("observed; verification/sync blocked: " if result["remote_landed"] else "unknown; reconcile before retry: ") + result["reason"]
            store.add_audit_event("window.fast_forward_partial", {"task_id": record.task_id, **result})
            progress(record, "refresh required" if result.get("refresh_required") else "blocked", str(result["reason"]))
        return result

    def merge_preflight():
        error = pulls.host_policy()
        if error:
            return error
        pending_records()  # read-only: ambiguous historical/pending landings must be visible in --plan too
        return None

    merge_record.preflight = merge_preflight
    merge_record.pending_records = pending_records
    merge_record.validate_records = validate_records
    from .landing_recovery import LandingRecovery
    recovery = LandingRecovery(store, project, pulls, base_ref, required_checks, merge_record)
    merge_record.recover = recovery.recover
    merge_record.resume_recovery = recovery.resume
    merge_record.active_recovery = recovery.active
    merge_record.recovery_preflight = recovery.preflight
    return merge_record
