"""Real local Git remotes, fake hosted observations; no provider or network."""
import time
from types import SimpleNamespace

import pytest
from autonomy_support import (
    TASK,
    add_passing_evidence,
    commit,
    git,
    init_repo,
    new_store,
    seed_candidate,
)

import stagemesh.fast_forward as ff
from stagemesh.autonomy.ci_diagnosis import Conclusion, FakeHostedCI, GateOutcome, HostedCIRun
from stagemesh.autonomy.dependencies import PullRequest
from stagemesh.fast_forward import FastForwardPullRequests, checks_ready
from stagemesh.survivor_wave import TaskRecord


def rig(tmp_path, monkeypatch, independent=True, intermediate=False):
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/widget.py": "OLD = 1"}, "base")
    git(repo, "checkout", "-b", "work")
    if intermediate:
        commit(repo, {"src/intermediate.py": "READY = True"}, "intermediate ancestor")
    candidate = commit(repo, {"src/widget.py": "NEW = 2"}, "candidate")
    remote = tmp_path / "origin.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-q")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", f"{base}:refs/heads/main", f"{candidate}:refs/heads/wave/task-{TASK}")
    git(repo, "checkout", "main")
    store = new_store(tmp_path)
    seed_candidate(store, TASK, baseline=base, candidate=candidate)
    add_passing_evidence(store, TASK, candidate, baseline=base, independent=independent)
    ci = FakeHostedCI([HostedCIRun(candidate, {"web": GateOutcome("web", Conclusion.SUCCESS)}),
                       HostedCIRun(base, {"web": GateOutcome("web", Conclusion.SUCCESS)})])
    adapter = FastForwardPullRequests("owner", "repo", None, ci)
    adapter.configure_ff(repo, store, "main", ("web",), ci)
    monkeypatch.setattr(adapter, "host_policy", lambda head_ref=None: None)
    monkeypatch.setattr(adapter, "bind_remote", lambda: None)
    adapter.expected_base = base
    pr = PullRequest(7, candidate, f"wave/task-{TASK}", "main", mergeable=True)
    monkeypatch.setattr(adapter, "get", lambda number: pr)
    monkeypatch.setattr(ff, "GitHubHostedCI", lambda *a: ci)
    monkeypatch.setattr(ff, "FastForwardPullRequests", lambda *a: adapter)
    from stagemesh import github
    from stagemesh.autonomy import cli
    monkeypatch.setattr(github, "detect_github_repository", lambda *a: SimpleNamespace(owner="owner", repo="repo"))
    monkeypatch.setattr(cli, "_github_token", lambda: None)
    return repo, remote, base, candidate, store, ci, adapter, pr


def test_supervised_ff_preserves_sha_lands_and_syncs_without_merge_commit(tmp_path, monkeypatch):
    repo, remote, base, sha, store, _ci, _adapter, _pr = rig(tmp_path, monkeypatch)
    merger = ff.make_merger(store, repo, "refs/heads/main", ("web",))
    record = TaskRecord(TASK, pr_url="https://github.com/owner/repo/pull/7", integrated_candidate=sha)
    result = merger(record, time.time() + 20)
    assert result["merged"], result
    assert git(remote, "rev-parse", "main") == sha == git(repo, "rev-parse", "main")
    assert git(repo, "rev-list", "--count", f"{base}..main") == "1"
    assert git(remote, "rev-parse", f"wave/task-{TASK}") == sha
    assert store.get_task(TASK)["stage"] == "DONE"


def test_supervisor_refuses_non_independent_review_before_push(tmp_path, monkeypatch):
    repo, remote, base, sha, store, _ci, _adapter, _pr = rig(tmp_path, monkeypatch, independent=False)
    record = TaskRecord(TASK, pr_url="https://github.com/owner/repo/pull/7", integrated_candidate=sha)
    result = ff.make_merger(store, repo, "refs/heads/main", ("web",))(record, time.time()+20)
    assert not result["merged"] and git(remote, "rev-parse", "main") == base


@pytest.mark.parametrize("failure", ["dirty", "head_moved", "base_moved", "paused", "missing_check", "ci_red", "deadline"])
def test_adapter_refuses_observed_unsafe_or_unverified_landing(tmp_path, monkeypatch, failure):
    repo, remote, base, sha, store, ci, adapter, _pr = rig(tmp_path, monkeypatch)
    if failure == "dirty":
        (repo / "src/widget.py").write_text("unexpected edit")
    elif failure == "head_moved":
        git(remote, "update-ref", f"refs/heads/wave/task-{TASK}", base)
    elif failure == "base_moved":
        git(remote, "update-ref", "refs/heads/main", sha)
    elif failure == "paused":
        from stagemesh.parallel import request_queue_control
        request_queue_control(store, "paused", reason="operator pause")
    elif failure == "deadline":
        adapter.deadline = 0
    elif failure == "missing_check":
        adapter.required_checks = ("web", "security")
    else:
        ci.runs[sha] = HostedCIRun(sha, {"web": GateOutcome("web", Conclusion.FAILURE)})
    before = git(remote, "rev-parse", "main")
    result = adapter.merge(7, sha, "ff-only")
    assert not result.merged
    assert git(remote, "rev-parse", "main") == before
    assert git(repo, "rev-parse", "main") == base


def test_missing_incomplete_or_wrong_sha_checks_do_not_count_as_green():
    sha = "a" * 40
    assert not checks_ready(FakeHostedCI(), sha, ("web",))
    ci = FakeHostedCI([HostedCIRun(sha, {"web": GateOutcome("web", Conclusion.SUCCESS)}, complete=False)])
    assert not checks_ready(ci, sha, ("web",))
    ci.runs[sha] = HostedCIRun("b" * 40, {"web": GateOutcome("web", Conclusion.SUCCESS)})
    assert not checks_ready(ci, sha, ("web",))


def test_local_sync_refuses_clean_main_ahead_of_the_reviewed_candidate(tmp_path, monkeypatch):
    repo, remote, _base, sha, _store, _ci, adapter, _pr = rig(tmp_path, monkeypatch)
    git(remote, "update-ref", "refs/heads/main", sha)
    git(repo, "checkout", "work")
    descendant = commit(repo, {"src/other.py": "OTHER = 3"}, "external local advance")
    git(repo, "checkout", "main")
    git(repo, "merge", "--ff-only", descendant)
    error = adapter.sync_local(sha)
    assert error and "moved outside" in error
    assert git(repo, "rev-parse", "main") == descendant


def test_remote_operations_have_a_bounded_timeout(tmp_path, monkeypatch):
    _repo, _remote, _base, _sha, _store, _ci, adapter, _pr = rig(tmp_path, monkeypatch)
    calls = []
    def bounded(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(ff.subprocess, "run", bounded)
    adapter.bounded_git("ls-remote", "origin", "refs/heads/main")
    assert 0 < calls[0][1]["timeout"] <= 20
    assert "--force" not in calls[0][0]


def test_pause_arriving_during_hosted_observation_prevents_push(tmp_path, monkeypatch):
    _repo, remote, base, sha, store, ci, adapter, _pr = rig(tmp_path, monkeypatch)
    original = ci.run_for
    def observe(commit_sha):
        from stagemesh.parallel import request_queue_control
        request_queue_control(store, "paused", reason="pause during CI")
        return original(commit_sha)
    monkeypatch.setattr(ci, "run_for", observe)
    result = adapter.merge(7, sha, "ff-only")
    assert not result.merged and result.reason == "queue_paused"
    assert git(remote, "rev-parse", "main") == base


def test_pause_arriving_in_final_remote_observation_prevents_push(tmp_path, monkeypatch):
    _repo, remote, base, sha, store, _ci, adapter, _pr = rig(tmp_path, monkeypatch)
    original = adapter.remote_tip
    calls = []
    def observe(ref, **kwargs):
        value = original(ref, **kwargs)
        calls.append(ref)
        if len(calls) == 4:
            from stagemesh.parallel import request_queue_control
            request_queue_control(store, "paused", reason="pause during final ref observation")
        return value
    monkeypatch.setattr(adapter, "remote_tip", observe)
    result = adapter.merge(7, sha, "ff-only")
    assert not result.merged and "queue_paused" in result.reason
    assert git(remote, "rev-parse", "main") == base


def test_supervised_ff_accepts_publisher_retry_branch(tmp_path, monkeypatch):
    from dataclasses import replace
    repo, remote, _base, sha, store, _ci, adapter, pr = rig(tmp_path, monkeypatch)
    retry_branch = f"wave/task-{TASK}-{sha[:7]}"
    git(repo, "push", "origin", f"{sha}:refs/heads/{retry_branch}")
    monkeypatch.setattr(adapter, "get", lambda number: replace(pr, head_ref=retry_branch))
    record = TaskRecord(TASK, pr_url="https://github.com/owner/repo/pull/7", integrated_candidate=sha)
    result = ff.make_merger(store, repo, "refs/heads/main", ("web",))(record, time.time() + 20)
    assert result["merged"], result
    assert git(remote, "rev-parse", "main") == sha == git(repo, "rev-parse", "main")
    assert git(remote, "rev-parse", retry_branch) == sha
    assert git(remote, "rev-parse", f"wave/task-{TASK}") == sha


@pytest.mark.parametrize("branch,expected", [
    ("wave/task-123", True),
    ("wave/task-123-abcdef0", True),
    ("wave/task-123-deadbee", False),
    ("wave/task-123-abcdef0-extra", False),
    ("wave/task-1234-abcdef0", False),
    ("other/task-123-abcdef0", False),
])
def test_task_pr_branch_requires_exact_publisher_identity(branch, expected):
    from stagemesh.survivor_wave import is_task_pr_branch
    assert is_task_pr_branch(branch, "123", "abcdef0123456789") is expected


def test_same_history_remote_advance_during_push_is_refused(tmp_path, monkeypatch):
    repo, remote, base, sha, _store, _ci, adapter, _pr = rig(tmp_path, monkeypatch, intermediate=True)
    middle = git(repo, "rev-parse", f"{sha}^")
    original = adapter.bounded_git
    def race(*args, **kwargs):
        if args[0] == "push":
            git(remote, "update-ref", "refs/heads/main", middle, base)
        return original(*args, **kwargs)
    monkeypatch.setattr(adapter, "bounded_git", race)
    result = adapter.merge(7, sha, "ff-only")
    assert not result.merged and "push_refused" in result.reason
    assert git(remote, "rev-parse", "main") == middle
    assert git(repo, "rev-parse", "main") == base


@pytest.mark.parametrize("failure", [None, "missing", "hidden_bypass", "bypass", "inactive", "inherited", "update_bypass", "PR_required"])
def test_host_policy_requires_enforced_unbypassable_immutable_heads(tmp_path, monkeypatch, failure):
    _repo, _remote, _base, _sha, _store, _ci, adapter, _pr = rig(tmp_path, monkeypatch)
    def request(method, path, **kwargs):
        if path.endswith("/protection"):
            return 404, {}
        if "/rules/branches/" in path:
            kinds = ["non_fast_forward"] if "/main?" in path else ["update", "deletion"]
            if failure == "missing":
                kinds = []
            if failure == "PR_required":
                kinds.append("pull_request")
            return 200, [{"type": kind, "ruleset_id": 1,
                "ruleset_source": "owner/repo",
                "ruleset_source_type": "Organization" if failure == "inherited" else "Repository",
                "parameters": {"update_allows_fetch_and_merge": failure == "update_bypass"}} for kind in kinds]
        policy = {"id": 1, "target": "branch", "rules": [{"type": k} for k in ("non_fast_forward", "update", "deletion", "pull_request")],
            "enforcement": "evaluate" if failure == "inactive" else "active", "bypass_actors": []}
        if failure == "hidden_bypass":
            policy.pop("bypass_actors")
        if failure == "bypass":
            policy["bypass_actors"] = [{"actor_type": "Integration", "actor_id": 1}]
        return 200, policy
    monkeypatch.setattr(adapter, "_request", request)
    assert (type(adapter).host_policy(adapter, "wave/task-TASK-1") is None) is (failure is None)


@pytest.mark.parametrize("problem", ["missing_ci", "non_independent", "missing_server_status"])
def test_already_merged_pr_cannot_bypass_current_policy(tmp_path, monkeypatch, problem):
    from dataclasses import replace

    from stagemesh.autonomy.dependencies import PRState
    repo, remote, base, sha, store, _ci, adapter, pr = rig(tmp_path, monkeypatch, independent=problem != "non_independent")
    git(remote, "update-ref", "refs/heads/main", sha)
    monkeypatch.setattr(adapter, "get", lambda number: replace(pr, state=PRState.MERGED, merge_commit_sha=sha))
    if problem == "missing_ci":
        adapter.required_checks = ("web", "security")
    if problem == "missing_server_status":
        adapter.hosted_requirements = {("contracts", None)}
        monkeypatch.setattr(adapter, "_request", lambda method, path: (200,
            {"check_runs": []} if "/check-runs?" in path else []))
    result = ff.make_merger(store, repo, "refs/heads/main", adapter.required_checks)(
        TaskRecord(TASK, pr_url="https://github.com/owner/repo/pull/7", integrated_candidate=sha), time.time() + 0.2)
    assert not result["merged"] and result["remote_landed"] is True
    assert git(repo, "rev-parse", "main") == base


def test_crash_after_push_is_journaled_and_resumable_after_pr_disappears(tmp_path, monkeypatch):
    from dataclasses import replace

    from stagemesh.autonomy.dependencies import PRState
    repo, _remote, base, sha, store, _ci, adapter, pr = rig(tmp_path, monkeypatch)
    original = adapter.bounded_git
    def crash(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == "push":
            raise TimeoutError("lost acknowledgment after push")
        return result
    monkeypatch.setattr(adapter, "bounded_git", crash)
    merger = ff.make_merger(store, repo, "refs/heads/main", ("web",))
    record = TaskRecord(TASK, pr_url="https://github.com/owner/repo/pull/7", integrated_candidate=sha)
    result = merger(record, time.time() + 20)
    assert not result["merged"] and result["remote_landed"] is True and result["local_synced"] is False
    assert git(repo, "rev-parse", "main") == base
    assert [r.integrated_candidate for r in merger.pending_records()] == [sha]
    monkeypatch.setattr(adapter, "bounded_git", original)
    monkeypatch.setattr(adapter, "get", lambda number: replace(pr, state=PRState.MERGED, merge_commit_sha=sha))
    resumed = merger(merger.pending_records()[0], time.time() + 20)
    assert resumed["merged"], resumed
    assert git(repo, "rev-parse", "main") == sha
    assert merger.pending_records() == []


@pytest.mark.parametrize("push_urls", ["https://github.com/owner/other.git\n", "https://github.com/owner/repo.git\nhttps://github.com/owner/other.git\n"])
def test_effective_push_destination_must_be_one_policy_verified_repository(tmp_path, monkeypatch, push_urls):
    _repo, _remote, _base, _sha, _store, _ci, adapter, _pr = rig(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter, "bounded_git", lambda *args: SimpleNamespace(
        stdout=push_urls if "--push" in args else "https://github.com/owner/repo.git\n"))
    with pytest.raises(ValueError):
        type(adapter).bind_remote(adapter)


@pytest.mark.parametrize("status,app_id,green", [(None, None, False), ("failure", None, False),
    ("success", None, True), ("success", 17, False)])
def test_required_legacy_status_and_app_checks_are_not_replaced_by_named_green_checks(tmp_path, monkeypatch, status, app_id, green):
    _repo, _remote, _base, sha, _store, _ci, adapter, _pr = rig(tmp_path, monkeypatch)
    adapter.hosted_requirements = {("contracts", app_id)}
    statuses = [] if status is None else [{"context": "contracts", "id": 1, "state": status}]
    monkeypatch.setattr(adapter, "_request", lambda method, path: (200,
        {"check_runs": []} if "/check-runs?" in path else statuses))
    assert adapter.required_statuses_ready(sha) is green


def test_recorded_landing_survives_subsequent_observation_failure(tmp_path, monkeypatch):
    repo, _remote, _base, sha, store, _ci, adapter, _pr = rig(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter, "sync_local", lambda sha: "local sync deliberately blocked")
    merger = ff.make_merger(store, repo, "refs/heads/main", ("web",))
    record = TaskRecord(TASK, pr_url="https://github.com/owner/repo/pull/7", integrated_candidate=sha)
    first = merger(record, time.time() + 20)
    assert first["historical_remote_landed"] and first["remote_landed"]
    monkeypatch.setattr(adapter, "get", lambda number: (_ for _ in ()).throw(OSError("GitHub unavailable")))
    second = merger(record, time.time() + 20)
    assert second["historical_remote_landed"] and second["remote_landed"] is True
    assert not second["merged"] and second["local_synced"] is False
