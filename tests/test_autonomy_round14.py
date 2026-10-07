"""Review round 14: a test-defect marker cannot excuse a production change, taint and already-landed work resolve themselves, proof is always required."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from autonomy_support import HUMAN, STAGEMESH, TASK, add_passing_evidence, commit, decisions, git, init_repo, new_store, seed_candidate
from test_autonomy_lifecycle import REF, _coordinator, _project, _task_at_integrate

from stagemesh.autonomy import base_state as base
from stagemesh.autonomy.ci_diagnosis import (
    CIClass,
    Conclusion,
    FakeHostedCI,
    GateOutcome,
    HostedCIRun,
    TestObservation,
    detect_test_defect,
    diagnose_ci,
    plan_ci_response,
)
from stagemesh.autonomy.decisions import Action, Condition
from stagemesh.autonomy.gitfacts import GitFacts
from stagemesh.autonomy.provenance import load_provenance
from stagemesh.autonomy.scope import TaskScope
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.execution import SubprocessExecutor
from stagemesh.workspaces import NO_IMPLEMENTATION_CHANGE

CAND, BASE = "c" * 40, "b" * 40
OWNER = "src/stagemesh/workspaces.py"


def _marker_log(test_id: str, path: str) -> str:
    marker = {"test_id": test_id, "test_path": path, "expected": "success", "observed_failure_code": NO_IMPLEMENTATION_CHANGE, "provider_made_change": False}
    return f"FAILED {test_id}\nSTAGEMESH_TEST_OBSERVATION {json.dumps(marker)}\n"


def _runs(test_id: str, path: str):
    candidate = HostedCIRun(CAND, {"unit": GateOutcome("unit", Conclusion.FAILURE, _marker_log(test_id, path), (test_id,))}, environment="x")
    return candidate, HostedCIRun(BASE, {"unit": GateOutcome("unit", Conclusion.SUCCESS)}, environment="x")


# --- a test-defect marker cannot excuse a candidate that changed the production behavior it blames ----------------------------------------------------------------------


def test_a_marker_is_honored_only_when_the_candidate_left_the_production_owner_files_alone() -> None:
    from stagemesh.autonomy.ci_diagnosis import observations_from_log

    candidate, base_run = _runs("t::noop", "tests/test_x.py")
    observations = observations_from_log(candidate.gates["unit"].log)
    honest = diagnose_ci(candidate, base_run, observations=observations, candidate_changed_files=("tests/test_x.py",))
    assert honest.gates[0].klass is CIClass.BROKEN_FRAGILE_TEST
    # the candidate edited the very production code the marker calls correct: the claim is not credible, the failure is the candidate's
    suspicious = diagnose_ci(candidate, base_run, observations=observations, candidate_changed_files=("tests/test_x.py", OWNER))
    assert suspicious.gates[0].klass is CIClass.CANDIDATE_REGRESSION
    assert plan_ci_response(suspicious, task_id=TASK).action is Action.REMEDIATE_CANDIDATE
    # unknown change set: nothing can be verified, so the marker is not trusted either
    unknown = diagnose_ci(candidate, base_run, observations=observations, candidate_changed_files=None)
    assert unknown.gates[0].klass is CIClass.CANDIDATE_REGRESSION


def test_the_supervisor_computes_the_changed_files_itself_from_git(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    base_sha = commit(repo, {"src/stagemesh/workspaces.py": "A = 1\n", "tests/test_x.py": "x\n"}, "base")
    git(repo, "checkout", "-q", "-b", "cand")
    honest = commit(repo, {"tests/test_x.py": "fixed fixture\n"}, "fix the fixture")
    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", "cheat", base_sha)
    cheat = commit(repo, {OWNER: "A = 2  # weakened production\n"}, "change production and blame the test")
    git(repo, "checkout", "-q", "main")
    store = new_store(tmp_path)
    store.upsert_task("t", source_id=TASK)
    ci = FakeHostedCI([*_runs("t::noop", "tests/test_x.py")])
    for sha, expected in ((honest, "CI_BROKEN_FRAGILE_TEST"), (cheat, "CI_CANDIDATE_REGRESSION")):
        candidate_run = HostedCIRun(sha, {"unit": GateOutcome("unit", Conclusion.FAILURE, _marker_log("t::noop", "tests/test_x.py"), ("t::noop",))}, environment="x")
        base_run = HostedCIRun(base_sha, {"unit": GateOutcome("unit", Conclusion.SUCCESS)}, environment="x")
        supervisor = Supervisor(store, repo, integration_ref="main", hosted_ci=FakeHostedCI([candidate_run, base_run]))
        decision = supervisor.assess_ci(TASK, sha, base_sha, scope=TaskScope("o", allowed_files=("tests/**",)))
        assert decision.condition.value == expected, (sha, decision.condition)
    assert detect_test_defect(TestObservation("t", "p", "success", NO_IMPLEMENTATION_CHANGE, False)) is not None


# --- tainted candidates are rebuilt, not stalled --------------------------------------------------------------------------------------------------------------------------


def test_a_tainted_candidate_sends_the_task_back_to_be_rebuilt_from_its_baseline(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    base_sha = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    worktree = repo / ".stagemesh" / "worktrees" / "wt"
    git(repo, "worktree", "add", "--detach", str(worktree), base_sha)
    foreign = commit(worktree, {"src/backdoor.py": "x\n"}, "adopted by mistake", who=HUMAN)
    candidate = commit(worktree, {"src/widget.py": "W = 1\n"}, "StageMesh commit on top of it", who=STAGEMESH)
    store = new_store(tmp_path)
    seed_candidate(store, TASK, baseline=base_sha, candidate=candidate, stage=Stage.INTEGRATE)
    add_passing_evidence(store, TASK, candidate, baseline=base_sha)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree)
    (worktree / "src" / "app.py").write_text("tampered\n", encoding="utf-8")

    decision = supervisor.check_workspace(TASK, execution_running=False)

    assert decision is not None and decision.detail["candidate_tainted"] is True
    assert decision.detail["recovery"] == "REBUILD_FROM_BASELINE"
    assert store.get_task(TASK)["stage"] == Stage.IMPLEMENT  # it does not sit revoked at INTEGRATE forever
    assert git(worktree, "rev-parse", "HEAD") == base_sha  # the worktree is back at the baseline; the tainted work is preserved in a ref
    assert git(repo, "rev-parse", f"refs/stagemesh/preserved/{__import__('stagemesh.workspaces', fromlist=['x'])._task_key(TASK)}/{candidate[:12]}") == candidate
    assert store.open_findings_for_candidate(TASK, candidate)
    assert not load_provenance(store, TASK).authorizes_integration() and foreign


# --- a candidate whose change already landed completes without a question -------------------------------------------------------------------------------------------


def test_a_candidate_whose_change_already_landed_independently_completes(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    git(project, "checkout", "-q", "-b", "candidate")
    candidate = commit(project, {"src/widget.py": "W = 1\n"}, "implement widget")
    git(project, "checkout", "-q", "main")
    from autonomy_support import seed_candidate as _seed
    from test_autonomy_lifecycle import CONTRACT

    _seed(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE, contract=CONTRACT)
    add_passing_evidence(store, TASK, candidate, baseline=base, contract=CONTRACT)
    commit(project, {"src/widget.py": "W = 1\n"}, "someone landed the identical change independently")
    supervisor = Supervisor(store, project, integration_ref=REF)
    coordinator = _coordinator(store, project, supervisor)

    for _ in range(6):
        coordinator.tick()

    assert store.get_task(TASK)["stage"] == Stage.DONE  # nothing was asked, nothing was rebuilt
    payload = json.loads(
        store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND kind=? AND status=?", (TASK, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED)
        ).fetchone()["payload"]
    )
    assert payload["integration_method"] == "content_already_on_base"
    assert any(d["condition"] == "CANDIDATE_ALREADY_INTEGRATED" and not d["requires_human"] for d in decisions(store, TASK))
    assert supervisor.provenance(TASK).integration_sha == git(project, "rev-parse", "main")


# --- the equivalence proof is always required ---------------------------------------------------------------------------------------------------------------------------


def test_a_replacement_that_touches_paths_the_candidate_never_touched_is_not_proven(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    old_base = commit(repo, {"a.txt": "a\n", "b.txt": "b\n"}, "base")
    git(repo, "checkout", "-q", "-b", "cand")
    candidate = commit(repo, {"a.txt": "A\n"}, "candidate edits a only")
    git(repo, "checkout", "-q", "main")
    new_base = commit(repo, {"c.txt": "c\n"}, "main moves")
    git(repo, "checkout", "-q", "-b", "wide", new_base)
    wide = commit(repo, {"a.txt": "A\n", "b.txt": "B sneaks in\n"}, "a replacement that also changes b")
    proof = base.prove_equivalence(GitFacts(repo), candidate=candidate, old_base=old_base, replacement=wide, new_base=new_base)
    assert not proof.proven and not proof.scope_preserved
    git(repo, "checkout", "-q", "-b", "narrow", new_base)
    narrow = commit(repo, {"a.txt": "A\n"}, "a replacement within the candidate's own paths")
    assert base.prove_equivalence(GitFacts(repo), candidate=candidate, old_base=old_base, replacement=narrow, new_base=new_base).scope_preserved
    assert sys and SubprocessExecutor and Condition and Action
