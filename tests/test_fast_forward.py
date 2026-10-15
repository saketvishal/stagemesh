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


def rig(tmp_path, monkeypatch, independent=True):
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/widget.py": "OLD = 1"}, "base")
    git(repo, "checkout", "-b", "work")
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


@pytest.mark.parametrize("state,resume", [("paused", False), ("stopping", False), ("stopping", True)])
def test_pause_arriving_during_hosted_observation_prevents_push(tmp_path, monkeypatch, state, resume):
    _repo, remote, base, sha, store, ci, adapter, _pr = rig(tmp_path, monkeypatch)
    original = ci.run_for
    def observe(commit_sha):
        from stagemesh.parallel import request_queue_control
        request_queue_control(store, state, reason="control during CI")
        if resume:
            request_queue_control(store, "resumed", reason="new run only")
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
