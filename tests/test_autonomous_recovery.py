"""Routine failures from the real Caventra run-window (tasks 143, 148, 172, 173) are repaired by StageMesh, not handed to the operator."""

from __future__ import annotations

import json
from pathlib import Path

from test_integration_recovery import (
    TASK,
    advance_until,
    coordinator,
    events,
    evidence_count,
    executions,
    land_on_main,
    tip,
)
from test_parallel import Rig, ScriptedExecutor

from stagemesh.domain import EvidenceKind, ExecutionKind, ExecutionStatus, Stage, TaskStatus
from stagemesh.git import GitWorkspace
from stagemesh.integration_recovery import BLOCKER_EVENT, RECOVERY_EVENT
from stagemesh.persistence import Store
from stagemesh.serialized_integration import REBASED_EVENT

FOREIGN = "carried/earlier_task.txt"


class StackedBaselineExecutor(ScriptedExecutor):
    """A task whose worktree started on an earlier task's unmerged commit (tasks 143 and 172: baseline commits not on main)."""

    name = "stacked"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path):
        from stagemesh.execution import ExecutionResult
        from stagemesh.workspaces import prepare_task_workspace, record_task_baseline

        execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION)
        run_path = prepare_task_workspace(project, task_id)
        (run_path / FOREIGN).parent.mkdir(parents=True, exist_ok=True)
        (run_path / FOREIGN).write_text("earlier task, as first built\n", encoding="utf-8")
        GitWorkspace(run_path).commit_all("earlier task's own commit")
        record_task_baseline(store, task_id, run_path)  # the contract baseline already carries the earlier task's commit
        rel, content = self.files[task_id]
        (run_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (run_path / rel).write_text(content, encoding="utf-8")
        sha = GitWorkspace(run_path).commit_all(f"implement {task_id}")
        store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)


def test_integration_failure_after_exact_sha_review_recovers_when_the_baseline_carries_foreign_commits(tmp_path: Path) -> None:
    """Task 172/143: reviewed candidate, main advanced; replaying the whole stack conflicted with the landed copy of an earlier task."""
    rig = Rig(tmp_path, [TASK])
    coord = coordinator(rig, StackedBaselineExecutor(rig.files))
    advance_until(rig, coord, Stage.INTEGRATE)
    original = rig.store.latest_candidate(TASK)["sha"]
    assert evidence_count(rig, EvidenceKind.REVIEW, original) == 1  # reviewed on its exact SHA
    # the earlier task reached main by another route (a squash with later edits), so its original commit would conflict if replayed
    land_on_main(rig, FOREIGN, "earlier task, squashed and since reworked\n")

    advance_until(rig, coord, Stage.DONE, 60)

    final = rig.store.latest_candidate(TASK)["sha"]
    assert final != original and tip(rig) == final
    assert GitWorkspace(rig.project).run("show", f"{rig.ref}:{FOREIGN}").stdout == "earlier task, squashed and since reworked\n"
    assert "out/A.txt" in rig.tree()
    (rebase,) = events(rig, REBASED_EVENT)
    assert rebase["from_candidate"] == original and rebase["candidate_sha"] == final
    assert not events(rig, RECOVERY_EVENT) and not events(rig, BLOCKER_EVENT)  # a mechanical refresh, no recovery attempt needed
    assert rig.store.get_task(TASK)["status"] != TaskStatus.BLOCKED
    # stale evidence never carries over: the refreshed SHA is validated and independently reviewed itself, then integrated once
    for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
        assert evidence_count(rig, kind, final) == 1, kind
    assert evidence_count(rig, EvidenceKind.INTEGRATION, original) == 0
    assert executions(rig, ExecutionKind.IMPLEMENTATION) == 1  # no provider re-run was needed
    assert executions(rig, ExecutionKind.REVIEW) == 2  # one review per SHA
    assert json.loads(rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type=?", (REBASED_EVENT,)).fetchone()[0])["onto"]


# --- the remaining required cases -------------------------------------------------------------------------------------------------


def test_the_design_conflict_block_is_a_human_decision_and_the_spent_budget_block_is_not(tmp_path: Path) -> None:
    from test_integration_recovery import redo_until_back_at_integrate

    from stagemesh.block_kinds import BlockKind, classify_block

    files = {TASK: ("shared.txt", "from A\n")}
    rig = Rig(tmp_path, [TASK], files=files)
    from test_integration_recovery import AppendingExecutor

    coord = coordinator(rig, AppendingExecutor(files))
    advance_until(rig, coord, Stage.INTEGRATE)
    land_on_main(rig, "shared.txt", "from elsewhere\n")
    redo_until_back_at_integrate(rig, coord)
    land_on_main(rig, "shared.txt", "rewritten by someone else\n")
    for _ in range(10):
        coord.tick()

    info = classify_block(rig.store, TASK)
    assert info.kind is BlockKind.HUMAN_DECISION and "design/spec conflict" in info.reason

    rig2 = Rig(tmp_path / "budget", [TASK])
    coord2 = coordinator(rig2, ScriptedExecutor(rig2.files), max_rebases=0, max_integration_recoveries=1)
    advance_until(rig2, coord2, Stage.INTEGRATE)
    land_on_main(rig2, "other/one.txt", "1\n")
    redo_until_back_at_integrate(rig2, coord2)
    land_on_main(rig2, "other/two.txt", "2\n")
    for _ in range(10):
        coord2.tick()
    info2 = classify_block(rig2.store, TASK)
    assert info2.kind is BlockKind.RECOVERABLE_OPERATIONAL and "spent (limit 1)" in info2.reason


def test_a_refresh_changes_only_the_task_files_and_never_forces_or_publishes(tmp_path: Path, monkeypatch) -> None:
    """No force-push, no history rewrite of the integration ref, no push at all, and no files outside the task's own change."""
    issued: list[tuple[str, ...]] = []
    real_run = GitWorkspace.run

    def recording(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        issued.append(tuple(str(a) for a in args))
        return real_run(self, *args, **kwargs)

    rig = Rig(tmp_path, [TASK])
    coord = coordinator(rig, StackedBaselineExecutor(rig.files))
    advance_until(rig, coord, Stage.INTEGRATE)
    land_on_main(rig, FOREIGN, "earlier task, squashed and since reworked\n")
    original_tip = tip(rig)
    monkeypatch.setattr(GitWorkspace, "run", recording)

    advance_until(rig, coord, Stage.DONE, 60)

    verbs = {call[0] for call in issued if call}
    assert "push" not in verbs and "filter-branch" not in verbs and "gc" not in verbs  # nothing is published or pruned
    for call in issued:
        if call and call[0] in {"branch", "update-ref", "tag"}:
            assert not {"--force", "-f", "-D", "-d", "--force-with-lease"} & set(call), call
        assert "--force-with-lease" not in call
    final = rig.store.latest_candidate(TASK)["sha"]
    changed = set(GitWorkspace(rig.project).run("diff", "--name-only", original_tip, final).stdout.split())
    assert changed == {"out/A.txt"}  # the landed foreign file is not touched; nothing outside the task's change was added
    assert GitWorkspace(rig.project).run("merge-base", "--is-ancestor", original_tip, final, check=False).returncode == 0  # main only moved forward


def test_integration_happens_once_per_candidate_and_never_twice(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    coord = coordinator(rig, StackedBaselineExecutor(rig.files))
    advance_until(rig, coord, Stage.INTEGRATE)
    land_on_main(rig, FOREIGN, "earlier task, squashed and since reworked\n")
    advance_until(rig, coord, Stage.DONE, 60)
    for _ in range(5):  # extra ticks on a finished task must not integrate again
        coord.tick()

    final = rig.store.latest_candidate(TASK)["sha"]
    passed = rig.store.conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE task_id=? AND kind=? AND status=?", (TASK, EvidenceKind.INTEGRATION, "PASSED")
    ).fetchone()[0]
    assert passed == 1 and evidence_count(rig, EvidenceKind.INTEGRATION, final) == 1
    assert not rig.store.conn.execute("SELECT 1 FROM claims WHERE active=1").fetchall()
    assert not rig.store.conn.execute("SELECT 1 FROM executions WHERE status='RUNNING'").fetchall()
    preserved = GitWorkspace(rig.project).run("for-each-ref", "refs/stagemesh/preserved").stdout
    assert preserved.strip()  # the original reviewed candidate stays reachable as history after the refresh


def test_a_recovery_round_changes_the_next_prompt_and_the_provider_choice(tmp_path: Path) -> None:
    """Exhaustion is answered with a different provider and a narrowed objective, not another identical attempt."""
    from test_bounded_execution import _setup

    from stagemesh.audit import record_audit
    from stagemesh.provider_pool import ProviderPool, Verdict
    from stagemesh.providers import _remediation_prompt
    from stagemesh.remediation import remediation_context
    from stagemesh.remediation_recovery import ROUND_EVENT, SWITCH_EVENT, avoided_providers

    _project, store = _setup(tmp_path)
    task = "TASK-1"
    store.add_candidate(task, "a" * 40, "codex", True)
    store.add_task_remediation(task, "REVIEW", "a" * 40)
    store.upsert_finding("f1", task, "a" * 40, "error", "Handle the empty input case")
    record_audit(store, SWITCH_EVENT, {"task_id": task, "avoid": ["codex"]})
    record_audit(store, ROUND_EVENT, {"task_id": task, "action": "REPAIR_WORKTREE", "round": 1})

    assert avoided_providers(store, task) == {"codex"}
    verdicts = ProviderPool._apply_recovery_switch(store, task, [Verdict("codex", True, "eligible"), Verdict("claude", True, "eligible")])
    assert [(v.provider, v.eligible) for v in verdicts] == [("codex", False), ("claude", True)]
    assert verdicts[0].reason.startswith("recovery_switch:")
    sole = ProviderPool._apply_recovery_switch(store, task, [Verdict("codex", True, "eligible"), Verdict("claude", False, "quota")])
    assert [(v.provider, v.eligible) for v in sole] == [("codex", True), ("claude", False)]  # never switches to nothing

    prompt = _remediation_prompt(remediation_context(store, task))
    assert "Handle the empty input case" in prompt and "Recovery attempt 1" in prompt and "do not continue an earlier approach" in prompt


# --- remediation budget (tasks 148 and 173) -----------------------------------------------------------------------------------------


def review_rig(tmp_path: Path, message: str, max_attempts: int = 2):  # type: ignore[no-untyped-def]
    import sys

    from test_bounded_execution import _setup
    from test_diagnosis import DOCS_CONTRACT, Attempts

    from stagemesh.coordinator import Coordinator, TargetSelection
    from stagemesh.diagnosis import DiagnosisPolicy
    from stagemesh.remediation import RemediationPolicy
    from stagemesh.review import Reviewer, ReviewFinding

    contract = dict(DOCS_CONTRACT, required_tests=[{"name": "docs-static-unit", "command": [sys.executable, "-c", "pass"]}])
    project, store = _setup(tmp_path, contract)
    executor = Attempts()
    reviewer = Reviewer(findings=[ReviewFinding("f1", "error", message)])
    coord = Coordinator(
        store, project, executor=executor, reviewer=reviewer, target=TargetSelection("TASK-1"),
        remediation_policy=RemediationPolicy(max_attempts=max_attempts), diagnosis_policy=DiagnosisPolicy(stop_on_repeat=False),
    )  # fmt: skip
    for _ in range(120):
        coord.tick()
        if store.get_task("TASK-1")["status"] in (TaskStatus.BLOCKED, TaskStatus.DONE):
            break
    return store, executor


def test_review_budget_exhaustion_runs_the_recovery_ladder_and_is_not_a_founder_decision(tmp_path: Path) -> None:
    from stagemesh.block_kinds import BlockKind, classify_block
    from stagemesh.survivor_wave import blocked_needs_decision

    store, executor = review_rig(tmp_path, "Handle the empty input case")

    assert store.get_task("TASK-1")["status"] == TaskStatus.BLOCKED
    assert executor.calls == 4  # first attempt + 2 budgeted remediations + 1 recovery round
    (round_,) = events_of(store, "recovery.exhaustion_round")
    assert round_["action"] == "REPAIR_WORKTREE" and round_["round"] == 1
    info = classify_block(store, "TASK-1")
    assert info.kind is BlockKind.RECOVERABLE_OPERATIONAL
    assert blocked_needs_decision(store, "TASK-1") is None  # "findings were not resolved within the budget" is not a question for a person
    exhausted = events_of(store, "task.remediation_exhausted")[-1]
    assert exhausted["block_kind"] == "BLOCKED_RECOVERABLE_OPERATIONAL" and exhausted["recoveries_attempted"] == ["REPAIR_WORKTREE"]
    assert "no healthy alternative remains" in exhausted["why_automatic_recovery_stopped"]
    decisions = events_of(store, "recovery.decision")
    assert [d["action"] for d in decisions] == ["REPAIR_WORKTREE", "BLOCK_EXTERNAL"] and {d["source"] for d in decisions} == {"rules"}


def test_a_finding_that_states_a_product_decision_is_asked_at_once_with_that_question(tmp_path: Path) -> None:
    from stagemesh.block_kinds import BlockKind, classify_block
    from stagemesh.survivor_wave import blocked_needs_decision

    question = "This needs a product decision: should expired matters keep their drafts for 30 or 90 days?"
    store, executor = review_rig(tmp_path, question)

    assert store.get_task("TASK-1")["status"] == TaskStatus.BLOCKED and executor.calls == 1  # no attempts are spent on a choice
    assert classify_block(store, "TASK-1").kind is BlockKind.HUMAN_DECISION
    assert "30 or 90 days" in blocked_needs_decision(store, "TASK-1")


def events_of(store: Store, event_type: str) -> list[dict]:
    rows = store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (event_type,))
    return [json.loads(r["payload"]) for r in rows]
