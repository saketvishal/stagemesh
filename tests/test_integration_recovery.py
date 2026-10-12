"""Routine integration failures are classified and repaired without the operator; the rest block with a typed decision."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_parallel import Rig, ScriptedExecutor

from stagemesh import serialized_integration
from stagemesh.contracts import bind_contract as make_bound_contract
from stagemesh.contracts import changed_files
from stagemesh.coordinator import Coordinator
from stagemesh.domain import (
    EvidenceKind,
    EvidenceStatus,
    ExecutionKind,
    ExecutionStatus,
    Stage,
    TaskStatus,
)
from stagemesh.git import GitWorkspace
from stagemesh.integration_recovery import (
    BLOCKER_EVENT,
    MERGE_CONFLICT,
    RECOVERY_EVENT,
    TARGET_ADVANCED,
    TRANSIENT,
    classify_integration_failure,
    integration_blocker,
)
from stagemesh.persistence import Store
from stagemesh.serialized_integration import RECONSTRUCTED_EVENT, SerializedIntegrator
from stagemesh.survivor_wave import blocked_needs_decision
from stagemesh.workspace_guard import owned_workspace
from stagemesh.workspaces import record_task_baseline

TASK = "A"


class AppendingExecutor(ScriptedExecutor):
    """Like a real provider: it holds the workspace lease, keeps what is already in the file it changes, adds its line and seals."""

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path):
        from stagemesh.execution import ExecutionResult
        from stagemesh.workspace_guard import owned_workspace
        from stagemesh.workspaces import record_task_baseline

        rel, content = self.files[task_id]
        execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION)
        with owned_workspace(store, project, task_id, ExecutionKind.IMPLEMENTATION, claim_id=claim_id) as lease:
            record_task_baseline(store, task_id, lease.path)
            target = lease.path / rel
            before = target.read_text(encoding="utf-8") if target.exists() else ""
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(before + content, encoding="utf-8")
            sha = GitWorkspace(lease.path).commit_all(f"implement {task_id}")
            store.add_candidate(task_id, sha, self.name, durable_handoff=True)
            store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
            lease.seal(sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)


def coordinator(rig: Rig, executor: ScriptedExecutor, *, max_rebases: int = 2, **kwargs) -> Coordinator:
    integrator = SerializedIntegrator(rig.ref, False, rig.lock, max_rebases=max_rebases)
    return Coordinator(rig.store, rig.project, executor=executor, integrator=integrator, **kwargs)


def advance_until(rig: Rig, coord: Coordinator, stage: Stage, limit: int = 40) -> None:
    for _ in range(limit):
        if rig.store.get_task(TASK)["stage"] == stage:
            return
        coord.tick()
    raise AssertionError(f"never reached {stage}: {dict(rig.store.get_task(TASK))}")


def land_on_main(rig: Rig, rel: str, content: str) -> str:
    (rig.project / rel).parent.mkdir(parents=True, exist_ok=True)
    (rig.project / rel).write_text(content, encoding="utf-8")
    return GitWorkspace(rig.project).commit_all("another task landed")


def tip(rig: Rig) -> str:
    return GitWorkspace(rig.project).run("rev-parse", rig.ref).stdout.strip()


def events(rig: Rig, event_type: str) -> list[dict]:
    rows = rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (event_type,))
    return [json.loads(r["payload"]) for r in rows]


def executions(rig: Rig, kind: ExecutionKind) -> int:
    return rig.store.conn.execute("SELECT COUNT(*) FROM executions WHERE task_id=? AND kind=?", (TASK, kind)).fetchone()[0]


def evidence_count(rig: Rig, kind: EvidenceKind, sha: str, status: EvidenceStatus = EvidenceStatus.PASSED) -> int:
    return rig.store.conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?", (TASK, sha, kind, status)
    ).fetchone()[0]


def seed_baseline_not_ancestor_failure(
    rig: Rig,
    *,
    files: dict[str, str],
    failure_files: list[str] | None = None,
    allowed_files: list[str] | None = None,
    foreign_files: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    """Create a reviewed candidate whose bound baseline is on side history, then put the integration ref on a different tip."""
    scope = allowed_files or sorted(files)
    git = GitWorkspace(rig.project)
    branch = rig.ref.rsplit("/", 1)[-1]
    git.run("checkout", "-b", "historical-baseline")
    (rig.project / "historical.txt").write_text("foreign historical baseline\n", encoding="utf-8")
    historical_baseline = git.commit_all("foreign historical baseline")
    git.run("checkout", branch)
    (rig.project / "current.txt").write_text("current integration tip\n", encoding="utf-8")
    current_tip = git.commit_all("current integration tip")
    (rig.project / ".stagemesh" / "contracts").mkdir(parents=True, exist_ok=True)
    (rig.project / ".stagemesh" / "contracts" / f"{TASK}.json").write_text(
        json.dumps(
            {
                "objective": f"touch {', '.join(scope)}",
                "explicit": True,
                "allowed_files": scope,
                "required_tests": [{"name": "smoke", "command": [sys.executable, "-c", "pass"]}],
            }
        ),
        encoding="utf-8",
    )

    with owned_workspace(rig.store, rig.project, TASK, ExecutionKind.IMPLEMENTATION) as lease:
        work = GitWorkspace(lease.path)
        work.run("checkout", "--detach", historical_baseline)
        record_task_baseline(rig.store, TASK, lease.path)
        for rel, content in (foreign_files or {}).items():
            target = lease.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if foreign_files:
            work.commit_all("foreign post-baseline change")
        for rel, content in files.items():
            target = lease.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        candidate = work.commit_all(f"implement {TASK}")
        rig.store.add_candidate(TASK, candidate, "scripted", durable_handoff=True)
        lease.seal(candidate)

    bound = make_bound_contract(rig.project, TASK, baseline_sha=historical_baseline, candidate_sha=candidate)
    rig.store.bind_task_contract(TASK, historical_baseline, bound.version, bound.digest, bound.canonical_json)
    rig.store.bind_contract(TASK, candidate, historical_baseline, bound.version, bound.digest, bound.canonical_json)
    rig.store.add_evidence(TASK, candidate, EvidenceKind.VALIDATION, EvidenceStatus.PASSED, bound.evidence_payload())
    rig.store.add_evidence(TASK, candidate, EvidenceKind.REVIEW, EvidenceStatus.PASSED, bound.evidence_payload())
    rig.store.add_evidence(
        TASK,
        candidate,
        EvidenceKind.INTEGRATION,
        EvidenceStatus.FAILED,
        {
            **bound.evidence_payload(),
            "findings": [
                {
                    "severity": "error",
                    "code": "integration_rebase_conflict",
                    "message": f"{rig.ref} advanced to {current_tip}; candidate {candidate} conflicts in {', '.join(failure_files or ['out/A.txt'])}; ref left unchanged",
                }
            ],
        },
    )
    rig.store.advance_task(TASK, Stage.INTEGRATE)
    return historical_baseline, current_tip, candidate


def redo_until_back_at_integrate(rig: Rig, coord: Coordinator) -> None:
    """Tick until one recovery happened and the redone candidate is waiting at INTEGRATE again."""
    for _ in range(60):
        coord.tick()
        if events(rig, RECOVERY_EVENT) and rig.store.get_task(TASK)["stage"] == Stage.INTEGRATE:
            return
    raise AssertionError(f"never back at INTEGRATE: {dict(rig.store.get_task(TASK))}")


def test_stale_main_after_review_is_refreshed_onto_the_current_base_and_lands_without_a_provider_rerun(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    coord = coordinator(rig, ScriptedExecutor(rig.files), max_rebases=0)  # no rebase budget: the ref moving past review is a typed failure
    advance_until(rig, coord, Stage.INTEGRATE)
    original = rig.store.latest_candidate(TASK)["sha"]
    moved = land_on_main(rig, "other/work.txt", "other task\n")

    advance_until(rig, coord, Stage.DONE, 60)

    final = rig.store.latest_candidate(TASK)["sha"]
    assert final != original and tip(rig) == final
    (recovery,) = events(rig, RECOVERY_EVENT)
    assert (recovery["category"], recovery["action"], recovery["attempt"]) == (TARGET_ADVANCED, "REFRESH_REVALIDATE_REREVIEW", 1)
    assert not events(rig, BLOCKER_EVENT)
    assert rig.store.contract_binding(TASK, final)["baseline_sha"] == moved  # refreshed onto what is on main now
    assert {"out/A.txt", "other/work.txt"} <= rig.tree()
    # the original candidate and its failed integration stay in the ledger as history
    assert rig.store.conn.execute("SELECT COUNT(*) FROM candidates WHERE task_id=? AND sha=?", (TASK, original)).fetchone()[0] == 1
    assert evidence_count(rig, EvidenceKind.INTEGRATION, original, EvidenceStatus.FAILED) == 1
    # the changed SHA gets its own validation, exact-SHA review and integration; nothing is carried over from the old SHA
    for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
        assert evidence_count(rig, kind, final) == 1, kind
    assert evidence_count(rig, EvidenceKind.INTEGRATION, original) == 0
    assert executions(rig, ExecutionKind.IMPLEMENTATION) == 1 and executions(rig, ExecutionKind.REVIEW) == 2  # only the exact-SHA review reran


def test_baseline_not_ancestor_reconstructs_task_delta_and_lands_without_replaying_historical_baseline(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    coord = coordinator(rig, ScriptedExecutor(rig.files))
    historical_baseline, current_tip, original = seed_baseline_not_ancestor_failure(rig, files={"out/A.txt": "A\n"})

    assert GitWorkspace(rig.project).run("merge-base", "--is-ancestor", historical_baseline, current_tip, check=False).returncode == 1
    assert coord._recover_integration(TASK, original) == 1
    reconstructed = rig.store.latest_candidate(TASK)["sha"]
    assert reconstructed != original
    assert rig.store.get_task(TASK)["stage"] == Stage.VALIDATE
    advance_until(rig, coord, Stage.DONE, 80)

    final = rig.store.latest_candidate(TASK)["sha"]
    assert final == reconstructed and tip(rig) == final
    assert "historical.txt" not in rig.tree()  # the side-baseline commit was not replayed
    assert {"current.txt", "out/A.txt"} <= rig.tree()
    (reconstructed_event,) = events(rig, RECONSTRUCTED_EVENT)
    assert reconstructed_event["from_candidate"] == original
    assert reconstructed_event["old_baseline"] == historical_baseline
    assert reconstructed_event["onto"] == current_tip
    (recovery,) = events(rig, RECOVERY_EVENT)
    assert (recovery["category"], recovery["action"]) == (MERGE_CONFLICT, "REPAIR_WORKTREE")
    assert evidence_count(rig, EvidenceKind.INTEGRATION, original, EvidenceStatus.FAILED) == 1
    for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
        assert evidence_count(rig, kind, reconstructed) == 1, kind
    assert rig.store.contract_binding(TASK, reconstructed)["baseline_sha"] == current_tip


def test_baseline_not_ancestor_reconstruction_refuses_ambiguous_out_of_scope_delta_and_preserves_history(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    coord = coordinator(rig, ScriptedExecutor(rig.files), max_integration_recoveries=1)
    _, current_tip, original = seed_baseline_not_ancestor_failure(
        rig,
        files={"out/A.txt": "A\n", "leaked.txt": "not task owned\n"},
        failure_files=["out/A.txt"],
        allowed_files=["out/A.txt"],
    )
    baseline = rig.store.contract_binding(TASK, original)["baseline_sha"]
    assert changed_files(rig.project, original, baseline) == ["leaked.txt", "out/A.txt"]

    assert coord._recover_integration(TASK, original) == 1

    assert rig.store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert rig.store.latest_candidate(TASK)["sha"] == original
    assert "leaked.txt" not in rig.tree() and tip(rig) == current_tip
    blocker = integration_blocker(rig.store, TASK)
    assert blocker["block_kind"] == "BLOCKED_POLICY_SAFETY"
    assert blocker["files"] == ["leaked.txt"]
    assert "outside the contract" in blocker["why_not_automatic"]
    assert len(events(rig, RECOVERY_EVENT)) == 1
    assert evidence_count(rig, EvidenceKind.VALIDATION, original) == 1
    assert evidence_count(rig, EvidenceKind.REVIEW, original) == 1
    assert evidence_count(rig, EvidenceKind.INTEGRATION, original, EvidenceStatus.FAILED) == 1


def test_baseline_not_ancestor_reconstruction_refuses_foreign_allowed_commits(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    coord = coordinator(rig, ScriptedExecutor(rig.files), max_integration_recoveries=1)
    _, current_tip, original = seed_baseline_not_ancestor_failure(
        rig,
        files={"out/A.txt": "task-owned\n"},
        failure_files=["out/A.txt"],
        allowed_files=["out/A.txt"],
        foreign_files={"out/A.txt": "foreign allowed-path change\n"},
    )

    assert coord._recover_integration(TASK, original) == 1

    assert rig.store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert rig.store.latest_candidate(TASK)["sha"] == original
    assert GitWorkspace(rig.project).run("show", f"{rig.ref}:out/A.txt", check=False).returncode != 0
    assert tip(rig) == current_tip
    blocker = integration_blocker(rig.store, TASK)
    assert blocker["block_kind"] == "BLOCKED_POLICY_SAFETY"
    assert "not recorded as candidates for this task" in blocker["why_not_automatic"]
    assert len(events(rig, RECOVERY_EVENT)) == 1


def test_baseline_not_ancestor_reconstruction_conflict_is_bounded_and_lists_files(tmp_path: Path) -> None:
    files = {TASK: ("shared.txt", "task version\n")}
    rig = Rig(tmp_path, [TASK], files=files)
    (rig.project / "shared.txt").write_text("base\n", encoding="utf-8")
    GitWorkspace(rig.project).commit_all("shared base")
    coord = coordinator(rig, ScriptedExecutor(files), max_integration_recoveries=1)
    _, _, original = seed_baseline_not_ancestor_failure(rig, files={"shared.txt": "task version\n"}, failure_files=["shared.txt"])
    (rig.project / "shared.txt").write_text("current version\n", encoding="utf-8")
    current_tip = GitWorkspace(rig.project).commit_all("current shared version")

    assert coord._recover_integration(TASK, original) == 1

    assert rig.store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert tip(rig) == current_tip
    blocker = integration_blocker(rig.store, TASK)
    assert blocker["classification"] == MERGE_CONFLICT
    assert blocker["files"] == ["shared.txt"]
    assert [a["action"] for a in blocker["recoveries_attempted"]] == ["REPAIR_WORKTREE"]
    assert blocker["block_kind"] == "BLOCKED_HUMAN_DECISION"
    assert "reconstructing the task-owned delta" in blocker["why_not_automatic"]
    assert len(events(rig, RECOVERY_EVENT)) == 1


def test_baseline_not_ancestor_reconstruction_apply_error_is_typed_without_workspace_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, [TASK])
    coord = coordinator(rig, ScriptedExecutor(rig.files), max_integration_recoveries=1)
    _, current_tip, original = seed_baseline_not_ancestor_failure(rig, files={"out/A.txt": "A\n"})
    real_run = serialized_integration.subprocess.run

    def fail_apply(*args, **kwargs):  # type: ignore[no-untyped-def]
        command = args[0] if args else kwargs.get("args", [])
        if "apply" in command:
            raise OSError("simulated apply failure after checkout")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(serialized_integration.subprocess, "run", fail_apply)

    assert coord._recover_integration(TASK, original) == 1

    assert rig.store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert rig.store.latest_candidate(TASK)["sha"] == original
    assert tip(rig) == current_tip
    blocker = integration_blocker(rig.store, TASK)
    assert blocker["block_kind"] == "BLOCKED_RECOVERABLE_OPERATIONAL"
    assert "simulated apply failure after checkout" in blocker["why_not_automatic"]
    mutation_blocks = [
        json.loads(row["payload"])
        for row in rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='task.blocked'")
    ]
    assert not mutation_blocks


def test_a_task_scoped_merge_conflict_is_redone_against_what_landed_and_lands(tmp_path: Path) -> None:
    files = {TASK: ("shared.txt", "from A\n")}
    rig = Rig(tmp_path, [TASK], files=files)
    coord = coordinator(rig, AppendingExecutor(files))
    advance_until(rig, coord, Stage.INTEGRATE)
    original = rig.store.latest_candidate(TASK)["sha"]
    land_on_main(rig, "shared.txt", "from elsewhere\n")  # touches the same file the candidate changed

    advance_until(rig, coord, Stage.DONE, 60)

    (recovery,) = events(rig, RECOVERY_EVENT)
    assert (recovery["category"], recovery["files"]) == (MERGE_CONFLICT, ["shared.txt"])
    final = rig.store.latest_candidate(TASK)["sha"]
    assert final != original and tip(rig) == final
    assert GitWorkspace(rig.project).run("show", f"{rig.ref}:shared.txt").stdout == "from elsewhere\nfrom A\n"  # both changes survive
    messages = [row["message"] for row in rig.store.conn.execute("SELECT message FROM findings WHERE task_id=?", (TASK,))]
    assert any("shared.txt" in message for message in messages)  # the provider is told which files to preserve
    assert not events(rig, BLOCKER_EVENT)


def test_a_conflict_that_comes_back_after_redoing_the_task_is_a_design_conflict_for_the_operator(tmp_path: Path) -> None:
    files = {TASK: ("shared.txt", "from A\n")}
    rig = Rig(tmp_path, [TASK], files=files)
    coord = coordinator(rig, AppendingExecutor(files))
    advance_until(rig, coord, Stage.INTEGRATE)
    land_on_main(rig, "shared.txt", "from elsewhere\n")
    redo_until_back_at_integrate(rig, coord)
    land_on_main(rig, "shared.txt", "rewritten by someone else\n")  # the same file moves again, incompatibly

    for _ in range(10):
        coord.tick()

    assert rig.store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    blocker = integration_blocker(rig.store, TASK)
    assert blocker["classification"] == MERGE_CONFLICT and blocker["files"] == ["shared.txt"]
    assert [a["action"] for a in blocker["recoveries_attempted"]] == ["REPAIR_WORKTREE"]
    assert "design/spec conflict" in blocker["why_not_automatic"]
    assert "which side wins" in blocker["decision_needed"]
    text = blocked_needs_decision(rig.store, TASK)
    assert "merge_conflict_with_current_target in shared.txt" in text and "decision needed" in text
    assert executions(rig, ExecutionKind.IMPLEMENTATION) == 2  # redone once, not retried forever
    assert tip(rig) != rig.store.latest_candidate(TASK)["sha"]  # nothing was forced onto the ref


def test_the_recovery_budget_is_bounded_and_the_blocker_lists_every_attempt(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    coord = coordinator(rig, ScriptedExecutor(rig.files), max_rebases=0, max_integration_recoveries=1)
    advance_until(rig, coord, Stage.INTEGRATE)
    land_on_main(rig, "other/one.txt", "1\n")
    redo_until_back_at_integrate(rig, coord)
    land_on_main(rig, "other/two.txt", "2\n")  # main moves again after the redo

    for _ in range(10):
        coord.tick()

    assert rig.store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert len(events(rig, RECOVERY_EVENT)) == 1
    blocker = integration_blocker(rig.store, TASK)
    assert blocker["classification"] == TARGET_ADVANCED
    assert "spent (limit 1)" in blocker["why_not_automatic"]
    assert [a["attempt"] for a in blocker["recoveries_attempted"]] == [1]
    assert executions(rig, ExecutionKind.IMPLEMENTATION) == 1  # the one recovery was a refresh, not a provider rerun


def test_a_transient_integration_failure_is_retried_and_integrates_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, [TASK])
    coord = coordinator(rig, ScriptedExecutor(rig.files))
    advance_until(rig, coord, Stage.INTEGRATE)
    sha = rig.store.latest_candidate(TASK)["sha"]
    real = SerializedIntegrator._fast_forward
    calls = {"n": 0}

    def flaky(self, project, candidate_sha, payload, findings):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            findings.append({"severity": "error", "code": "integration_ref_update_failed", "message": "could not lock ref"})
            return
        real(self, project, candidate_sha, payload, findings)

    monkeypatch.setattr(SerializedIntegrator, "_fast_forward", flaky)

    advance_until(rig, coord, Stage.DONE, 60)
    for _ in range(4):  # extra ticks must not integrate again
        coord.tick()

    (recovery,) = events(rig, RECOVERY_EVENT)
    assert (recovery["category"], recovery["action"]) == (TRANSIENT, "RETRY_INTEGRATION")
    assert rig.store.latest_candidate(TASK)["sha"] == sha and tip(rig) == sha
    assert executions(rig, ExecutionKind.IMPLEMENTATION) == 1  # the provider did not run again
    assert evidence_count(rig, EvidenceKind.INTEGRATION, sha) == 1 and calls["n"] == 2
    assert executions(rig, ExecutionKind.INTEGRATION) == 2


def test_ledger_and_worktrees_stay_consistent_after_a_recovery(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    coord = coordinator(rig, ScriptedExecutor(rig.files), max_rebases=0)
    advance_until(rig, coord, Stage.INTEGRATE)
    land_on_main(rig, "other/work.txt", "x\n")

    advance_until(rig, coord, Stage.DONE, 60)

    assert not rig.store.conn.execute("SELECT 1 FROM claims WHERE active=1").fetchall()
    assert not rig.store.conn.execute("SELECT 1 FROM executions WHERE status=?", (ExecutionStatus.RUNNING,)).fetchall()
    dirty = [line for line in GitWorkspace(rig.project).run("status", "--porcelain").stdout.splitlines() if ".stagemesh" not in line]
    assert dirty == []  # nothing but StageMesh runtime files changed in the project checkout
    refs = GitWorkspace(rig.project).run("for-each-ref", "refs/stagemesh/preserved").stdout
    assert refs.strip(), "the superseded worktree state is preserved under refs/stagemesh/preserved"


@pytest.mark.parametrize(
    ("code", "category"),
    [("integration_rebase_unavailable", TRANSIENT), ("integration_stale_base", TARGET_ADVANCED), ("integration_rebase_conflict", MERGE_CONFLICT)],
)
def test_only_typed_integration_failures_are_classified(tmp_path: Path, code: str, category: str) -> None:
    rig = Rig(tmp_path, [TASK])
    sha, other = "c" * 40, "d" * 40
    for candidate, findings in ((sha, [{"code": code, "message": "x"}]), (other, [{"code": "missing_required_bound_evidence", "message": "x"}, {"code": code, "message": "x"}])):
        rig.store.add_candidate(TASK, candidate, "scripted", True)
        rig.store.add_evidence(TASK, candidate, EvidenceKind.INTEGRATION, EvidenceStatus.FAILED, {"findings": findings})

    assert classify_integration_failure(rig.store, TASK, sha).category == category
    assert classify_integration_failure(rig.store, TASK, other) is None  # mixed with a non-integration finding: ordinary remediation
