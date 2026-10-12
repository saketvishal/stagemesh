from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

from test_parallel import Rig, ScriptedExecutor

import stagemesh.cli as cli_module
from stagemesh.config import load_config
from stagemesh.coordinator import Coordinator
from stagemesh.domain import (
    EvidenceKind,
    EvidenceStatus,
    ExecutionKind,
    Stage,
    TaskStatus,
)
from stagemesh.git import GitWorkspace
from stagemesh.serialized_integration import SerializedIntegrator
from stagemesh.workspace_guard import record_supervised_move

TASK = "A"


def _single_task_coordinator(rig: Rig, executor: ScriptedExecutor) -> Coordinator:
    """Exactly what `continue --task A` builds (see cli._build_coordinator): a SerializedIntegrator, no parallel runner."""
    integrator = SerializedIntegrator(rig.ref, False, rig.lock, max_rebases=2)
    return Coordinator(rig.store, rig.project, executor=executor, integrator=integrator)


def _advance_until(rig: Rig, coord: Coordinator, stage: Stage, limit: int = 30) -> None:
    for _ in range(limit):
        if rig.store.get_task(TASK)["stage"] == stage:
            return
        coord.tick()
    raise AssertionError(f"never reached {stage}: {dict(rig.store.get_task(TASK))}")


def _land_on_main(rig: Rig, rel: str, content: str) -> str:
    git = GitWorkspace(rig.project)
    (rig.project / rel).parent.mkdir(parents=True, exist_ok=True)
    (rig.project / rel).write_text(content, encoding="utf-8")
    return git.commit_all("another task landed")


def _tip(rig: Rig) -> str:
    return GitWorkspace(rig.project).run("rev-parse", rig.ref).stdout.strip()


def _executions(rig: Rig, kind: ExecutionKind) -> int:
    return rig.store.conn.execute("SELECT COUNT(*) FROM executions WHERE task_id=? AND kind=?", (TASK, kind)).fetchone()[0]


def test_continue_wires_the_serialized_integrator_for_a_single_task(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    loaded = load_config(rig.project)
    coord, info = cli_module._build_coordinator(Namespace(dry_run=False), rig.project, loaded, rig.store, None, lambda *a, **k: None)
    assert isinstance(coord.integrator, SerializedIntegrator)
    assert coord.integrator.integration_ref == info["integration_ref"]
    assert coord.integrator.max_rebases == loaded.parallel.integration_rebase_attempts


def test_clean_stale_candidate_is_rebased_then_revalidated_and_integrated(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    coord = _single_task_coordinator(rig, executor)
    _advance_until(rig, coord, Stage.INTEGRATE)
    original = rig.store.latest_candidate(TASK)["sha"]
    original_binding = rig.store.contract_binding(TASK, original)
    tip = _land_on_main(rig, "other/work.txt", "other task\n")  # the integration ref advances past the candidate's base

    coord.tick()  # integrate: stale -> rebase, not a failure and not back to IMPLEMENT

    task = rig.store.get_task(TASK)
    rebased = rig.store.latest_candidate(TASK)["sha"]
    assert (task["stage"], task["status"]) == (Stage.VALIDATE, TaskStatus.OPEN)
    assert rebased != original
    assert GitWorkspace(rig.project).run("merge-base", "--is-ancestor", tip, rebased, check=False).returncode == 0
    assert _tip(rig) == tip  # nothing landed yet
    # the same frozen contract is bound to the rebased commit, with the new tip as its baseline
    binding = rig.store.contract_binding(TASK, rebased)
    assert binding["digest"] == original_binding["digest"] and binding["version"] == original_binding["version"]
    assert binding["baseline_sha"] == tip
    # nothing is carried over: the rebased candidate has no passing evidence yet
    for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
        assert not rig.store.has_bound_evidence(TASK, rebased, kind, binding["digest"], EvidenceStatus.PASSED)

    _advance_until(rig, coord, Stage.DONE)

    for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
        assert rig.store.has_bound_evidence(TASK, rebased, kind, binding["digest"], EvidenceStatus.PASSED)
    assert _executions(rig, ExecutionKind.VALIDATION) == 2 and _executions(rig, ExecutionKind.REVIEW) == 2
    assert _executions(rig, ExecutionKind.IMPLEMENTATION) == 1  # the provider never ran again
    assert {"out/A.txt", "other/work.txt"} <= rig.tree()
    assert _tip(rig) == rebased


def test_true_rebase_conflict_refreshes_baseline_then_reimplements_to_done(tmp_path: Path) -> None:
    files = {TASK: ("shared.txt", "from A\n")}
    rig = Rig(tmp_path, [TASK], files=files)

    class Reconciler(ScriptedExecutor):
        def run(self, store, task_id, claim_id, project):
            if len([item for item in self.log if item[0] == "start" and item[1] == task_id]) == 0:
                return super().run(store, task_id, claim_id, project)
            self.files[task_id] = ("shared.txt", "from elsewhere\nfrom A\n")
            baseline = store.task_baseline(task_id)
            result = super().run(store, task_id, claim_id, project)
            assert baseline is not None and result.candidate_sha is not None
            assert record_supervised_move(project, task_id, baseline, result.candidate_sha, result.candidate_sha, require_same_dirty=False)
            return result

    executor = Reconciler(files)
    coord = _single_task_coordinator(rig, executor)
    _advance_until(rig, coord, Stage.INTEGRATE)
    original = rig.store.latest_candidate(TASK)["sha"]
    tip = _land_on_main(rig, "shared.txt", "from elsewhere\n")

    coord.tick()  # integrate: the rebase conflicts, the ref is left exactly as it was
    assert _tip(rig) == tip
    codes = set()
    for row in rig.store.conn.execute("SELECT payload FROM evidence WHERE task_id=? AND kind=?", (TASK, EvidenceKind.INTEGRATION)):
        codes |= {f["code"] for f in json.loads(row["payload"]).get("findings", [])}
    assert "integration_rebase_conflict" in codes  # the failed attempt stays in the ledger

    for _ in range(60):
        coord.tick()
        if rig.store.get_task(TASK)["stage"] == Stage.DONE:
            break

    assert rig.store.get_task(TASK)["stage"] == Stage.DONE
    assert GitWorkspace(rig.project).run("show", f"{rig.ref}:shared.txt").stdout == "from elsewhere\nfrom A\n"
    assert _executions(rig, ExecutionKind.IMPLEMENTATION) == 2
    assert rig.store.latest_candidate(TASK)["sha"] != original
    assert rig.store.conn.execute("SELECT COUNT(*) FROM candidates WHERE task_id=? AND sha=?", (TASK, original)).fetchone()[0] == 1
    assert GitWorkspace(rig.project).run("merge-base", "--is-ancestor", tip, _tip(rig), check=False).returncode == 0
    events = [json.loads(row["payload"]) for row in rig.store.audit_events() if row["event_type"] == "task.baseline_advanced"]
    assert any(str(event.get("reason", "")).startswith("integration recovery: merge_conflict_with_current_target") for event in events)
    assert any(row["event_type"] == "integration.recovery" for row in rig.store.audit_events())  # recorded and counted against the recovery budget
    owner_state = [json.loads(row["payload"]) for row in rig.store.audit_events() if row["event_type"] == "task.owner_state"][-1]
    assert owner_state["current_problem"] is None and owner_state["unresolved_findings"] == []
