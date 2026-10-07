"""A StageMesh base refresh must update the workspace ledger, or the merged ownership guard blocks the replacement."""

from __future__ import annotations

from test_autonomy_lifecycle import CONTRACT, REF, _project

from autonomy_support import TASK, add_passing_evidence, commit, git, seed_candidate

from stagemesh.autonomy.decisions import Action
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.domain import ExecutionKind, Stage
from stagemesh.workspace_guard import owned_workspace, verify_candidate_workspace
from stagemesh.workspaces import prepare_task_workspace


def test_refreshing_the_base_records_the_move_in_the_workspace_ledger(tmp_path) -> None:
    project, store, base = _project(tmp_path)
    git(project, "checkout", "-q", "-b", "candidate")
    candidate = commit(project, {"src/widget.py": "W = 1\n"}, "implement widget")
    git(project, "checkout", "-q", "main")
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.REVIEW, contract=CONTRACT)
    add_passing_evidence(store, TASK, candidate, baseline=base, contract=CONTRACT)
    worktree = prepare_task_workspace(project, TASK)
    git(worktree, "checkout", "-q", "--detach", candidate)
    supervisor = Supervisor(store, project, integration_ref=REF)
    supervisor.claim_workspace(TASK, worktree)
    with owned_workspace(store, project, TASK, ExecutionKind.IMPLEMENTATION) as lease:
        lease.check("before_agent")
        lease.seal(candidate)

    commit(project, {"docs/notes.md": "unrelated\n"}, "main moves")
    decision = supervisor.reconcile_base(TASK)

    assert decision is not None and decision.action is Action.REFRESH_CANDIDATE
    replacement = decision.shas["replacement_candidate"]
    verify_candidate_workspace(store, project, TASK, replacement, "VALIDATE")


def test_refresh_keeps_a_sealed_untracked_file_and_records_the_new_head(tmp_path) -> None:
    project, store, base = _project(tmp_path)
    git(project, "checkout", "-q", "-b", "candidate")
    candidate = commit(project, {"src/widget.py": "W = 1\n"}, "implement widget")
    git(project, "checkout", "-q", "main")
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.REVIEW, contract=CONTRACT)
    add_passing_evidence(store, TASK, candidate, baseline=base, contract=CONTRACT)
    worktree = prepare_task_workspace(project, TASK)
    git(worktree, "checkout", "-q", "--detach", candidate)
    supervisor = Supervisor(store, project, integration_ref=REF)
    supervisor.claim_workspace(TASK, worktree)
    with owned_workspace(store, project, TASK, ExecutionKind.IMPLEMENTATION) as lease:
        lease.check("before_agent")
        (worktree / "notes.tmp").write_text("keep\n", encoding="utf-8")
        lease.seal(candidate)
    supervisor.execution_finished(TASK)

    commit(project, {"docs/notes.md": "unrelated\n"}, "main moves")
    decision = supervisor.reconcile_base(TASK)

    assert decision is not None and decision.action is Action.REFRESH_CANDIDATE
    replacement = decision.shas["replacement_candidate"]
    assert (worktree / "notes.tmp").read_text(encoding="utf-8") == "keep\n"
    verify_candidate_workspace(store, project, TASK, replacement, "VALIDATE")


def test_reconstruct_does_not_seal_an_embedded_repo_clean_left_behind(tmp_path) -> None:
    project, store, base = _project(tmp_path)
    git(project, "checkout", "-q", "-b", "candidate")
    candidate = commit(project, {"src/widget.py": "W = 1\n"}, "implement widget")
    git(project, "checkout", "-q", "main")
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.REVIEW, contract=CONTRACT)
    add_passing_evidence(store, TASK, candidate, baseline=base, contract=CONTRACT)
    worktree = prepare_task_workspace(project, TASK)
    git(worktree, "checkout", "-q", "--detach", candidate)
    supervisor = Supervisor(store, project, integration_ref=REF)
    supervisor.claim_workspace(TASK, worktree)
    with owned_workspace(store, project, TASK, ExecutionKind.IMPLEMENTATION) as lease:
        lease.check("before_agent")
        lease.seal(candidate)

    nested = worktree / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "payload.py").write_text("SECRET = 1\n", encoding="utf-8")
    git(nested, "add", "payload.py")
    git(nested, "-c", "user.email=other@example.invalid", "-c", "user.name=Other", "commit", "-q", "-m", "foreign")

    supervisor._reset_idle_worktree(TASK, base)

    from stagemesh.autonomy.provenance import load_ownership
    from stagemesh.workspace_guard import _gitdir, _load_ledger

    ledger, _raw = _load_ledger(_gitdir(worktree))
    assert ledger is not None and ledger["head"] == candidate
    assert "nested/" not in (ledger.get("dirty") or {})
    assert load_ownership(store, TASK).expected_head == candidate
