"""End-to-end: the real Coordinator, executor, validator, reviewer and integrator running with the supervisor wired in.

These prove the first-milestone scenarios act inside the actual lifecycle (not only through the supervisor API): a second writer is
never adopted (A), a stale candidate is refreshed and re-evidenced until DONE (B), and a force-rewritten base with identical trees is
retargeted with provenance and re-evidenced until DONE (C).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from autonomy_support import (
    TASK,
    add_passing_evidence,
    commit,
    decisions,
    git,
    init_repo,
    seed_candidate,
    tree_of,
)

from stagemesh.autonomy.integration import SupervisedIntegrator
from stagemesh.autonomy.provenance import load_provenance
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.concurrency import IntegrationLock
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.execution import SubprocessExecutor
from stagemesh.persistence import Store

REF = "refs/heads/main"  # the production form of the integration ref
SMOKE = {"name": "smoke", "command": [sys.executable, "-c", "pass"]}
CONTRACT = {
    "objective": "add the widget",
    "allowed_files": ["src/**"],
    "acceptance_criteria": ["widget exists"],
    "required_tests": [SMOKE],
}


def _project(tmp_path: Path, *, autonomy: bool = True) -> tuple[Path, Store, str]:
    project = init_repo(tmp_path / "project")
    git(project, "config", "user.email", "stagemesh@example.invalid")
    git(project, "config", "user.name", "StageMesh")
    (project / ".git" / "info" / "exclude").write_text(".stagemesh/\n", encoding="utf-8")  # as in every real project (.gitignore)
    base = commit(project, {"src/app.py": "VALUE = 1\n", "docs/a.md": "a\n"}, "base")
    runtime = project / ".stagemesh"
    (runtime / "contracts").mkdir(parents=True)
    (runtime / "contracts" / f"{TASK}.json").write_text(json.dumps(CONTRACT), encoding="utf-8")
    if autonomy:
        (runtime / "autonomy.json").write_text(json.dumps({"enabled": True}), encoding="utf-8")
    store = Store(runtime / "stagemesh.sqlite3")
    store.migrate()
    return project, store, base


def _coordinator(store: Store, project: Path, supervisor: Supervisor, executor=None) -> Coordinator:
    integrator = SupervisedIntegrator(supervisor, REF, False, IntegrationLock(project / ".stagemesh" / "integration.lock"), max_rebases=2)
    return Coordinator(store, project, executor=executor, integrator=integrator, guard=supervisor)


def _drive(coordinator: Coordinator, store: Store, max_ticks: int = 12) -> int:
    ticks = 0
    while ticks < max_ticks and store.get_task(TASK)["stage"] != Stage.DONE:
        coordinator.tick()
        ticks += 1
    return ticks


_PROVIDER = """
import os, subprocess
from pathlib import Path
marker = Path('../attempt.marker')
first = not marker.exists()
marker.write_text('x')
if first:
    # a second writer commits into this execution-owned worktree while the agent is working
    Path('src/backdoor.py').write_text('SECRET = 1\\n')
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'Other Dev', 'GIT_AUTHOR_EMAIL': 'other.dev@example.com',
           'GIT_COMMITTER_NAME': 'Other Dev', 'GIT_COMMITTER_EMAIL': 'other.dev@example.com'}
    subprocess.run(['git', 'add', '-A'], check=True)
    subprocess.run(['git', 'commit', '-qm', 'second writer'], check=True, env=env)
else:
    Path('src/widget.py').write_text('W = 1\\n')
"""


def test_scenario_a_end_to_end_second_writer_commit_is_never_adopted_into_the_candidate(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    store.upsert_task("add the widget", source_id=TASK)
    store.advance_task(TASK, Stage.IMPLEMENT)
    script = tmp_path / "provider.py"
    script.write_text(_PROVIDER, encoding="utf-8")
    supervisor = Supervisor(store, project, integration_ref=REF)
    coordinator = _coordinator(store, project, supervisor, executor=SubprocessExecutor([sys.executable, str(script)], name="codex"))

    assert coordinator.tick() == 0  # the first attempt is refused: nothing the second writer did becomes a candidate

    assert store.latest_candidate(TASK) is None
    (failure,) = [e for e in store.audit_events() if e["event_type"] == "task.implementation_unsuccessful"]
    assert "EXTERNAL_WORKSPACE_MUTATION" in json.loads(failure["payload"])["reason"]
    (decision,) = decisions(store, TASK)
    assert decision["condition"] == "EXTERNAL_WORKSPACE_MUTATION" and decision["requires_human"] is False
    ref = decision["detail"]["quarantine_refs"]["worktree_snapshot"]
    assert git(project, "log", "-1", "--format=%s", ref) == "second writer"  # preserved for inspection, not lost
    worktree = Path(decision["observed"]["workspace"])
    assert git(worktree, "rev-parse", "HEAD") == base  # restored to what StageMesh recorded

    assert coordinator.tick() == 1  # the next attempt, from the clean workspace, produces a real candidate
    candidate = store.latest_candidate(TASK)["sha"]
    assert store.get_task(TASK)["stage"] == Stage.VALIDATE
    assert "second writer" not in git(project, "log", "--format=%s", candidate)
    assert subprocess.run(["git", "cat-file", "-e", f"{candidate}:src/backdoor.py"], cwd=project, capture_output=True).returncode != 0


def test_opted_out_projects_are_unaffected_by_the_hooks(tmp_path: Path) -> None:
    project, store, _base = _project(tmp_path, autonomy=False)
    store.upsert_task("add the widget", source_id=TASK)
    store.advance_task(TASK, Stage.IMPLEMENT)
    script = tmp_path / "provider.py"
    script.write_text(_PROVIDER, encoding="utf-8")
    coordinator = Coordinator(store, project, executor=SubprocessExecutor([sys.executable, str(script)], name="codex"))
    coordinator.tick()
    assert decisions(store, TASK) == []  # no supervisor, no behavior change (the legacy path adopts whatever is in the worktree)


# --- Scenario B and C through to DONE -----------------------------------------------------------------------------------------------------


def _task_at_integrate(store: Store, project: Path, base: str) -> str:
    git(project, "checkout", "-q", "-b", "candidate")
    candidate = commit(project, {"src/widget.py": "W = 1\n"}, "implement widget")
    git(project, "checkout", "-q", "main")
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE, contract=CONTRACT)
    add_passing_evidence(store, TASK, candidate, baseline=base, contract=CONTRACT)
    return candidate


def _evidence(store: Store, sha: str, kind: EvidenceKind, status: EvidenceStatus = EvidenceStatus.PASSED) -> bool:
    return store.has_evidence(TASK, sha, kind, status)


def test_scenario_b_end_to_end_stale_candidate_is_refreshed_reevidenced_and_integrated(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    original = _task_at_integrate(store, project, base)
    main_tip = commit(project, {"docs/notes.md": "unrelated normal commit\n"}, "main advances normally")
    supervisor = Supervisor(store, project, integration_ref=REF)
    coordinator = _coordinator(store, project, supervisor)

    _drive(coordinator, store)

    task = store.get_task(TASK)
    assert task["stage"] == Stage.DONE and task["status"] != TaskStatus.BLOCKED
    replacement = store.latest_candidate(TASK)["sha"]
    assert replacement != original
    # the replacement got its own validation, independent-of-original review and integration evidence
    assert _evidence(store, replacement, EvidenceKind.VALIDATION)
    assert _evidence(store, replacement, EvidenceKind.REVIEW)
    assert _evidence(store, replacement, EvidenceKind.INTEGRATION)
    assert git(project, "rev-parse", "main") == replacement  # integrated by fast-forward, never force-pushed
    assert git(project, "rev-parse", f"{replacement}^") == main_tip
    assert git(project, "rev-parse", "candidate") == original  # original untouched
    provenance = load_provenance(store, TASK)
    assert provenance.lineage == (original, replacement) and provenance.integration_sha == replacement
    verified = [d for d in decisions(store, TASK) if d["condition"] == "POST_MERGE_VERIFIED"]
    assert verified and verified[-1]["action"] == "MARK_DONE"
    assert [d["action"] for d in decisions(store, TASK)][0] == "REFRESH_CANDIDATE"


def test_scenario_c_end_to_end_equivalent_tree_rewrite_is_retargeted_reevidenced_and_integrated(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    original = _task_at_integrate(store, project, base)
    rewritten = git(project, "commit-tree", tree_of(project, base), "-m", "main rewritten (squash)")
    git(project, "update-ref", "refs/heads/main", rewritten)
    supervisor = Supervisor(store, project, integration_ref=REF)
    coordinator = _coordinator(store, project, supervisor)

    _drive(coordinator, store)

    assert store.get_task(TASK)["stage"] == Stage.DONE
    replacement = store.latest_candidate(TASK)["sha"]
    assert replacement != original and git(project, "rev-parse", f"{replacement}^") == rewritten
    assert tree_of(project, replacement) == tree_of(project, original)
    assert _evidence(store, replacement, EvidenceKind.VALIDATION) and _evidence(store, replacement, EvidenceKind.REVIEW)
    assert git(project, "rev-parse", "main") == replacement and git(project, "rev-parse", "candidate") == original
    first = decisions(store, TASK)[0]
    assert first["condition"] == "BASE_HISTORY_REWRITTEN" and first["action"] == "CREATE_RETARGETED_CANDIDATE"
    assert first["detail"]["proof"]["proven"] is True and first["requires_human"] is False


def test_evidence_for_the_original_candidate_cannot_authorize_the_replacement(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    original = _task_at_integrate(store, project, base)
    commit(project, {"docs/notes.md": "unrelated\n"}, "main advances")
    supervisor = Supervisor(store, project, integration_ref=REF)
    coordinator = _coordinator(store, project, supervisor)

    coordinator.tick()  # INTEGRATE: refreshed, sent back to VALIDATE

    replacement = store.latest_candidate(TASK)["sha"]
    assert store.get_task(TASK)["stage"] == Stage.VALIDATE
    assert _evidence(store, original, EvidenceKind.VALIDATION) and _evidence(store, original, EvidenceKind.REVIEW)
    assert not _evidence(store, replacement, EvidenceKind.VALIDATION) and not _evidence(store, replacement, EvidenceKind.REVIEW)
    assert not load_provenance(store, TASK).authorizes_integration()
    assert git(project, "rev-parse", "main") != replacement  # nothing integrated yet


def test_a_candidate_that_conflicts_with_main_is_reconstructed_not_force_merged(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    git(project, "checkout", "-q", "-b", "candidate")
    candidate = commit(project, {"src/app.py": "VALUE = 2\n"}, "change app")
    git(project, "checkout", "-q", "main")
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE, contract=CONTRACT)
    add_passing_evidence(store, TASK, candidate, baseline=base, contract=CONTRACT)
    commit(project, {"src/app.py": "VALUE = 3\n"}, "main changes the same line")
    supervisor = Supervisor(store, project, integration_ref=REF)
    coordinator = _coordinator(store, project, supervisor)

    coordinator.tick()

    assert store.get_task(TASK)["stage"] == Stage.IMPLEMENT  # re-implement on the new base; the founder was not asked anything
    assert [d["action"] for d in decisions(store, TASK)] == ["RECONSTRUCT_ON_NEW_BASE"]
    assert git(project, "rev-parse", "main") != candidate


# --- Scenarios E and F inside the lifecycle (hosted CI gate before integration) -------------------------------------------------------------


def _ci_supervisor(store: Store, project: Path, runs: list) -> Supervisor:
    from stagemesh.autonomy.ci_diagnosis import FakeHostedCI

    return Supervisor(store, project, integration_ref=REF, hosted_ci=FakeHostedCI(runs))


def _gate(name: str, conclusion: str, log: str = "", tests: tuple[str, ...] = ()):
    from stagemesh.autonomy.ci_diagnosis import Conclusion, GateOutcome

    return GateOutcome(name, Conclusion(conclusion), log, tests)


def _run(sha: str, *gates):
    from stagemesh.autonomy.ci_diagnosis import HostedCIRun

    return HostedCIRun(sha, {g.name: g for g in gates})


def test_scenario_e_end_to_end_base_already_red_does_not_block_integration(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    candidate = _task_at_integrate(store, project, base)
    red = "FAILED tests/test_legacy.py::test_old - AssertionError"
    supervisor = _ci_supervisor(
        store,
        project,
        [
            _run(candidate, _gate("legacy", "failure", red, ("tests/test_legacy.py::test_old",)), _gate("unit", "success")),
            _run(base, _gate("legacy", "failure", red, ("tests/test_legacy.py::test_old",)), _gate("unit", "success")),
        ],
    )
    coordinator = _coordinator(store, project, supervisor)

    _drive(coordinator, store)

    assert store.get_task(TASK)["stage"] == Stage.DONE
    assert git(project, "rev-parse", "main") == candidate
    ci = [d for d in decisions(store, TASK) if d["condition"] == "CI_BASELINE_FAILURE"]
    assert ci and ci[0]["action"] == "RECORD_BASELINE_FAILURE_AND_PROCEED" and ci[0]["requires_human"] is False
    from stagemesh.autonomy.scope import deferred_items

    assert [item.source for item in deferred_items(store, TASK)] == ["ci"]  # recorded as deferred, never "fixed"


def test_scenario_f_end_to_end_new_failure_is_remediated_without_touching_baseline_failures(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    candidate = _task_at_integrate(store, project, base)
    supervisor = _ci_supervisor(
        store,
        project,
        [
            _run(candidate, _gate("unit", "failure", tests=("tests/test_widget.py::test_new",)), _gate("lint", "failure", "E501 too long")),
            _run(base, _gate("unit", "success"), _gate("lint", "failure", "E501 too long")),
        ],
    )
    coordinator = _coordinator(store, project, supervisor)

    coordinator.tick()

    assert store.get_task(TASK)["stage"] == Stage.IMPLEMENT  # automatically remediated; main untouched
    assert git(project, "rev-parse", "main") == base
    (finding,) = store.open_findings_for_candidate(TASK, candidate)
    assert "unit" in finding["message"] and "tests/test_widget.py::test_new" in finding["message"]
    assert "Do not touch gates that already fail on base: lint" in finding["message"]
    assert [d["condition"] for d in decisions(store, TASK)] == ["CI_CANDIDATE_REGRESSION"]


def test_scenario_f_remediation_budget_exhaustion_is_a_typed_specific_escalation(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    candidate = _task_at_integrate(store, project, base)
    for _ in range(3):
        store.add_task_remediation(TASK, "INTEGRATE", candidate)
    supervisor = _ci_supervisor(
        store, project, [_run(candidate, _gate("unit", "failure", tests=("t::x",))), _run(base, _gate("unit", "success"))]
    )

    coordinator = _coordinator(store, project, supervisor)
    coordinator.tick()

    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    escalation = decisions(store, TASK)[-1]["escalation"]
    assert escalation["reason"] == "REMEDIATION_BUDGET_EXHAUSTED"
    assert escalation["attempted"] and escalation["why_undeterminable"]
    assert "unit" in escalation["smallest_decision"] and escalation["smallest_decision"].endswith("?")
