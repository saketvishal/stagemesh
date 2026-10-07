"""Reconstruction must not authorize an embedded Git repository that Git itself ignores."""

from __future__ import annotations

from autonomy_support import TASK, add_passing_evidence, commit, git, seed_candidate
from test_autonomy_lifecycle import CONTRACT, REF, _project

from stagemesh.autonomy.provenance import load_ownership
from stagemesh.domain import ExecutionKind, Stage
from stagemesh.workspace_guard import (
    WorkspaceMutation,
    _gitdir,
    _load_ledger,
    owned_workspace,
    unexpected_embedded_repositories,
    verify_candidate_workspace,
)
from stagemesh.workspaces import prepare_task_workspace


def _owned(tmp_path):
    project, store, base = _project(tmp_path)
    git(project, "checkout", "-q", "-b", "candidate")
    candidate = commit(project, {"src/widget.py": "W = 1\n"}, "implement widget")
    git(project, "checkout", "-q", "main")
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.REVIEW, contract=CONTRACT)
    add_passing_evidence(store, TASK, candidate, baseline=base, contract=CONTRACT)
    worktree = prepare_task_workspace(project, TASK)
    git(worktree, "checkout", "-q", "--detach", candidate)
    from stagemesh.autonomy.supervisor import Supervisor

    supervisor = Supervisor(store, project, integration_ref=REF)
    supervisor.claim_workspace(TASK, worktree)
    with owned_workspace(store, project, TASK, ExecutionKind.IMPLEMENTATION) as lease:
        lease.check("before_agent")
        lease.seal(candidate)
    return project, store, base, candidate, worktree, supervisor


def _git_dir_repo(path) -> None:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "payload.py").write_text("SECRET = 1\n", encoding="utf-8")
    git(path, "add", "payload.py")
    git(path, "-c", "user.email=other@example.invalid", "-c", "user.name=Other", "commit", "-q", "-m", "foreign")


def _assert_not_authorized(store, project, worktree, candidate, base) -> None:
    ledger, _raw = _load_ledger(_gitdir(worktree))
    assert ledger is not None and ledger["head"] == candidate
    assert load_ownership(store, TASK).expected_head == candidate
    try:
        verify_candidate_workspace(store, project, TASK, base, "IMPLEMENT")
    except WorkspaceMutation:
        return
    raise AssertionError("reconstruction authorized a worktree that still holds an embedded repository")


def test_reconstruct_does_not_authorize_an_ignored_embedded_repository(tmp_path) -> None:
    project, store, base, candidate, worktree, supervisor = _owned(tmp_path)
    nested = worktree / ".stagemesh" / "nested"
    _git_dir_repo(nested)
    git(worktree, "check-ignore", "-q", ".stagemesh/nested/payload.py")
    cleaned = git(worktree, "clean", "-fdn", check=False)
    assert "nested" not in cleaned

    supervisor._reset_idle_worktree(TASK, base)

    assert (nested / "payload.py").read_text(encoding="utf-8") == "SECRET = 1\n"
    assert ".stagemesh/nested" in unexpected_embedded_repositories(worktree)
    _assert_not_authorized(store, project, worktree, candidate, base)


def test_reconstruct_does_not_authorize_an_ignored_gitfile_worktree(tmp_path) -> None:
    project, store, base, candidate, worktree, supervisor = _owned(tmp_path)
    real = tmp_path / "foreign-gitdir"
    _git_dir_repo(real)
    linked = worktree / ".stagemesh" / "linked"
    linked.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {real.resolve().as_posix()}\n", encoding="utf-8")
    (linked / "payload.py").write_text("SECRET = 2\n", encoding="utf-8")

    supervisor._reset_idle_worktree(TASK, base)

    assert (linked / ".git").is_file()
    assert ".stagemesh/linked" in unexpected_embedded_repositories(worktree)
    _assert_not_authorized(store, project, worktree, candidate, base)


def test_a_task_worktree_gitlink_is_not_an_embedded_repository(tmp_path) -> None:
    _project_path, _store, _base, _candidate, worktree, _supervisor = _owned(tmp_path)
    assert (worktree / ".git").exists()
    assert unexpected_embedded_repositories(worktree) == []
