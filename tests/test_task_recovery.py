"""Task recovery commands (rebind-contract, rebaseline-task, task-doctor) and the diagnosis/propagation they rely on.

The scenarios are modelled on a real operator incident (a task whose contract was hand-edited with a wrong stored version, whose
baseline went stale after another task integrated, and which looped on validation/review) but are plain fixtures: nothing in the
product knows about that project.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
from stagemesh.baseline import analyze_baseline
from stagemesh.contract_binding import bind_task_contract, contract_for_candidate
from stagemesh.contracts import CONTRACT_VERSION, ContractError, changed_files
from stagemesh.coordinator import Coordinator, TargetSelection
from stagemesh.diagnosis import STALE_BASELINE, VALIDATION_GATE, diagnose
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, Stage, TaskStatus
from stagemesh.git import GitWorkspace
from stagemesh.providers import _remediation_prompt
from stagemesh.audit import record_audit
from stagemesh.autonomy.provenance import claim_workspace, load_ownership
from stagemesh.recovery import RecoveryRefusal, advance_baseline, rebaseline_task, rebind_contract, task_doctor
from stagemesh.remediation import finding_identity, remediation_context
from stagemesh.run_ready import RunSummary, drive_task, format_stop
from stagemesh.validation import Validator
from stagemesh.workspaces import prepare_task_workspace

from test_bounded_execution import TASK, _setup as _base_setup
from test_diagnosis import DOCS_CONTRACT as _DOCS, Attempts, drive, events

PY = sys.executable
# a docs-only change plans a "docs-static" check, which must exist as a gate for the validation to be able to pass
DOCS_CONTRACT = {**_DOCS, "required_tests": [{"name": "docs-static", "command": [sys.executable, "-c", "pass"]}]}


def _setup(tmp_path: Path, contract: dict | None = None):
    project, store = _base_setup(tmp_path, contract)
    exclude = project / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".stagemesh/" + chr(10), encoding="utf-8")  # runtime state must never land in the commits these fixtures make
    return project, store


def cli(project: Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_module.main(["--project", str(project), *argv])
    return code, out.getvalue(), err.getvalue()


def write_contract(project: Path, contract: dict) -> None:
    (project / ".stagemesh" / "contracts" / f"{TASK}.json").write_text(json.dumps(contract), encoding="utf-8")


def head(project: Path) -> str:
    return GitWorkspace(project).head()


def stale_baseline_task(tmp_path: Path):
    """Task baseline recorded, then ANOTHER task integrates web-v2 files, then this task's candidate is built on the new tip."""
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    old_base = store.set_task_baseline(TASK, head(project))
    (project / "web-v2").mkdir()
    (project / "web-v2" / "page.tsx").write_text("export default 1\n", encoding="utf-8")
    (project / "web-v2" / "util.ts").write_text("export const x = 1\n", encoding="utf-8")
    GitWorkspace(project).commit_all("another task integrated web-v2")
    integrated = head(project)
    run_path = prepare_task_workspace(project, TASK)
    (run_path / "docs" / "a.md").write_text("changed by the task\n", encoding="utf-8")
    sha = GitWorkspace(run_path).commit_all("the task's own change")
    store.add_candidate(TASK, sha, "provider", durable_handoff=True)
    store.advance_task(TASK, Stage.VALIDATE)
    return project, store, old_base, integrated, sha


def ref_of(project: Path) -> str:
    return GitWorkspace(project).run("symbolic-ref", "-q", "HEAD").stdout.strip()


def frozen_task_with_candidate(tmp_path: Path, contract: dict | None = None):
    project, store = _setup(tmp_path, contract or DOCS_CONTRACT)
    base = store.set_task_baseline(TASK, head(project))
    bind_task_contract(store, project, TASK, base)
    (project / "docs" / "a.md").write_text("candidate\n", encoding="utf-8")
    sha = GitWorkspace(project).commit_all("candidate")
    store.add_candidate(TASK, sha, "provider", durable_handoff=True)
    store.advance_task(TASK, Stage.VALIDATE)
    return project, store, base, sha


# --- 1. rebind-contract ------------------------------------------------------------------------------------------------------------


def test_rebind_repairs_a_hand_edited_contract_version_and_keeps_the_supported_version(tmp_path: Path) -> None:
    project, store, base, sha = frozen_task_with_candidate(tmp_path)
    contract_for_candidate(store, TASK, sha, project)  # the candidate binding exists
    # the incident: the operator edits the contract file, then "fixes" the DB by hand and bumps the version to 2
    widened = {**DOCS_CONTRACT, "allowed_files": ["docs/**", "src/**"]}
    write_contract(project, widened)
    store.conn.execute("UPDATE task_contracts SET version=2 WHERE task_id=?", (TASK,))
    store.conn.execute("UPDATE contract_bindings SET version=2 WHERE task_id=?", (TASK,))
    store.conn.commit()
    with pytest.raises(ContractError, match="unsupported bound contract version: 2"):
        contract_for_candidate(store, TASK, sha, project)

    report = rebind_contract(store, project, TASK, reason="allow src for the helper")

    assert report["old_version"] == 2 and report["new_version"] == CONTRACT_VERSION == 1 and report["version_repaired"]
    assert report["digest_changed"] and report["old_digest"] != report["new_digest"]
    assert store.task_contract(TASK)["version"] == 1 and store.contract_binding(TASK, sha)["version"] == 1
    bound = contract_for_candidate(store, TASK, sha, project)  # no longer raises
    assert bound.version == 1 and bound.contract.allowed_files == ("docs/**", "src/**") and bound.digest == report["new_digest"]
    (event,) = events(store, "task.contract_rebound")
    assert event["old_digest"] == report["old_digest"] and event["new_digest"] == report["new_digest"]
    assert (event["old_version"], event["new_version"]) == (2, 1)
    assert event["operator_action"] == "REBIND_CONTRACT" and event["reason"] == "allow src for the helper" and event["operator"]
    assert event["old_canonical_json"]  # the replaced contract text is kept in the audit trail


def test_rebind_does_not_copy_the_version_from_the_stored_row_even_when_the_digest_is_unchanged(tmp_path: Path) -> None:
    project, store, _, sha = frozen_task_with_candidate(tmp_path)
    store.conn.execute("UPDATE task_contracts SET version=2 WHERE task_id=?", (TASK,))
    store.conn.commit()
    report = rebind_contract(store, project, TASK)
    assert report["digest_changed"] is False and report["version_repaired"] and store.task_contract(TASK)["version"] == 1


def test_rebind_with_validate_validates_the_latest_candidate_and_advances(tmp_path: Path) -> None:
    project, store, _, sha = frozen_task_with_candidate(tmp_path, {**DOCS_CONTRACT, "allowed_files": ["src/**"]})
    # under the frozen contract the docs-only candidate violates scope
    assert Validator().validate(store, TASK, sha, project) is EvidenceStatus.FAILED
    store.block_task(TASK)
    write_contract(project, DOCS_CONTRACT)
    report = rebind_contract(store, project, TASK, validate=True)
    assert report["validation"]["status"] == "PASSED" and report["validation"]["advanced"] is True
    task = store.get_task(TASK)
    assert (task["stage"], task["status"]) == (Stage.REVIEW, TaskStatus.OPEN) and report["unblocked"] is True
    # history is preserved: the earlier failed evidence and its finding are still there
    assert store.has_bound_evidence(TASK, sha, EvidenceKind.VALIDATION, report["old_digest"], EvidenceStatus.FAILED)
    assert store.conn.execute("SELECT COUNT(*) FROM findings WHERE task_id=?", (TASK,)).fetchone()[0] >= 1


def test_rebind_refuses_with_an_active_claim_or_running_execution(tmp_path: Path) -> None:
    project, store, _, _ = frozen_task_with_candidate(tmp_path)
    claim = store.acquire_claim(TASK, "w1")
    with pytest.raises(RecoveryRefusal) as claimed:
        rebind_contract(store, project, TASK)
    assert claimed.value.code == "active_claim"
    store.release_claim(claim)
    store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.VALIDATION)
    with pytest.raises(RecoveryRefusal) as running:
        rebind_contract(store, project, TASK)
    assert running.value.code == "running_execution"
    assert not events(store, "task.contract_rebound")


def test_rebind_refuses_to_silently_invalidate_passed_evidence_and_keeps_it_when_forced(tmp_path: Path) -> None:
    project, store, _, sha = frozen_task_with_candidate(tmp_path)
    assert Validator().validate(store, TASK, sha, project) is EvidenceStatus.PASSED
    write_contract(project, {**DOCS_CONTRACT, "allowed_files": ["docs/**", "src/**"]})
    with pytest.raises(RecoveryRefusal) as refused:
        rebind_contract(store, project, TASK)
    assert refused.value.code == "history_invalidated" and refused.value.detail["evidence_ids"]
    assert store.task_contract(TASK)["digest"] == contract_for_candidate(store, TASK, sha, project).digest  # nothing changed
    report = rebind_contract(store, project, TASK, force=True)
    assert report["forced"] and report["invalidated_evidence"] == refused.value.detail["evidence_ids"]
    assert store.conn.execute("SELECT COUNT(*) FROM evidence WHERE task_id=?", (TASK,)).fetchone()[0] == 1  # evidence kept


def test_rebind_refuses_missing_or_invalid_contract_file(tmp_path: Path) -> None:
    project, store, _, _ = frozen_task_with_candidate(tmp_path)
    (project / ".stagemesh" / "contracts" / f"{TASK}.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RecoveryRefusal) as bad:
        rebind_contract(store, project, TASK)
    assert bad.value.code == "invalid_contract"
    (project / ".stagemesh" / "contracts" / f"{TASK}.json").unlink()
    with pytest.raises(RecoveryRefusal) as missing:
        rebind_contract(store, project, TASK)
    assert missing.value.code == "missing_contract_file"
    assert store.task_contract(TASK)["version"] == 1  # untouched


def test_rebind_cli_json_and_refusal_exit_code(tmp_path: Path) -> None:
    project, store, _, sha = frozen_task_with_candidate(tmp_path)
    write_contract(project, {**DOCS_CONTRACT, "allowed_files": ["docs/**", "lib/**"]})
    store.close()
    code, out, _ = cli(project, "rebind-contract", "--task", TASK, "--json")
    data = json.loads(out)
    assert code == 0 and data["new_version"] == 1 and data["digest_changed"] and data["operator_action"] == "REBIND_CONTRACT"
    code, _, err = cli(project, "rebind-contract", "--task", "nope")
    assert code == 2 and "task does not exist" in err
    code, out, _ = cli(project, "rebind-contract", "--task", "nope", "--json")
    assert code == 2 and json.loads(out)["code"] == "unknown_task"


# --- 2. rebaseline-task ------------------------------------------------------------------------------------------------------------


def test_stale_baseline_makes_unrelated_files_look_out_of_scope_and_rebaseline_removes_them(tmp_path: Path) -> None:
    project, store, old_base, integrated, sha = stale_baseline_task(tmp_path)
    ref = ref_of(project)

    analysis = analyze_baseline(store, project, TASK, ref)
    assert analysis.stale and analysis.baseline_sha == old_base and analysis.merge_base == integrated
    assert analysis.changed_before == ["docs/a.md", "web-v2/page.tsx", "web-v2/util.ts"] and analysis.changed_after == ["docs/a.md"]
    assert analysis.unrelated == ["web-v2/page.tsx", "web-v2/util.ts"]
    assert Validator().validate(store, TASK, sha, project) is EvidenceStatus.FAILED
    failed = json.loads(store.conn.execute("SELECT payload FROM evidence WHERE kind='VALIDATION'").fetchone()[0])
    assert {f["path"] for f in failed["findings"] if f["code"] == "outside_allowed_files"} == {"web-v2/page.tsx", "web-v2/util.ts"}

    report = rebaseline_task(store, project, TASK, ref, validate=True, reason="task 61 integrated first")

    assert report["old_baseline"] == old_base and report["new_baseline"] == integrated and report["candidate_sha"] == sha
    assert report["changed_files_before"] == ["docs/a.md", "web-v2/page.tsx", "web-v2/util.ts"] and report["changed_files_after"] == ["docs/a.md"]
    assert report["validation"]["status"] == "PASSED" and report["validation"]["advanced"] and report["stage"] == Stage.REVIEW
    assert store.task_baseline(TASK) == integrated and store.task_contract(TASK) is None or store.task_contract(TASK)["baseline_sha"] == integrated
    assert store.contract_binding(TASK, sha)["baseline_sha"] == integrated
    (event,) = events(store, "task.rebaselined")
    assert event["old_baseline"] == old_base and event["new_baseline"] == integrated and event["candidate_sha"] == sha
    assert event["changed_files_before"] != event["changed_files_after"] and event["operator_action"] == "REBASELINE_TASK"
    assert event["reason"] == "task 61 integrated first" and event["forced"] is False
    # the failed evidence and its findings are history, not deleted
    assert store.conn.execute("SELECT COUNT(*) FROM evidence WHERE status='FAILED'").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM findings WHERE task_id=?", (TASK,)).fetchone()[0] >= 2


def test_rebaseline_refuses_active_claim_running_execution_and_not_stale(tmp_path: Path) -> None:
    project, store, old_base, integrated, sha = stale_baseline_task(tmp_path)
    ref = ref_of(project)
    claim = store.acquire_claim(TASK, "w1")
    with pytest.raises(RecoveryRefusal) as claimed:
        rebaseline_task(store, project, TASK, ref)
    assert claimed.value.code == "active_claim" and store.task_baseline(TASK) == old_base
    store.release_claim(claim)
    execution = store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.VALIDATION)
    with pytest.raises(RecoveryRefusal) as running:
        rebaseline_task(store, project, TASK, ref)
    assert running.value.code == "running_execution"
    store.finish_execution(execution, "FAILED")
    rebaseline_task(store, project, TASK, ref)
    with pytest.raises(RecoveryRefusal) as again:  # now the baseline is current: nothing to do
        rebaseline_task(store, project, TASK, ref)
    assert again.value.code == "not_stale" and len(events(store, "task.rebaselined")) == 1


def test_rebaseline_refuses_when_the_candidate_is_already_integrated_even_with_force(tmp_path: Path) -> None:
    project, store, old_base, _, sha = stale_baseline_task(tmp_path)
    GitWorkspace(project).run("merge", "--ff-only", sha)  # the candidate lands on the integration ref
    with pytest.raises(RecoveryRefusal) as refused:
        rebaseline_task(store, project, TASK, ref_of(project), force=True)
    assert refused.value.code == "candidate_already_integrated" and store.task_baseline(TASK) == old_base


def test_rebaseline_refuses_ambiguous_history_unless_forced(tmp_path: Path) -> None:
    project, store, old_base, integrated, sha = stale_baseline_task(tmp_path)
    # the recorded baseline is a commit the candidate does not descend from (a side branch)
    git = GitWorkspace(project)
    git.run("checkout", "-q", "-b", "side", old_base)
    (project / "side.txt").write_text("s\n", encoding="utf-8")
    side = git.commit_all("side branch commit")
    git.run("checkout", "-q", "-")
    store.conn.execute("UPDATE task_baselines SET baseline_sha=? WHERE task_id=?", (side, TASK))
    store.conn.commit()
    ref = ref_of(project)
    with pytest.raises(RecoveryRefusal) as refused:
        rebaseline_task(store, project, TASK, ref)
    assert refused.value.code == "candidate_not_based_on_baseline" and "--force" in str(refused.value)
    assert store.task_baseline(TASK) == side and not events(store, "task.rebaselined")
    report = rebaseline_task(store, project, TASK, ref, force=True)
    assert report["forced"] is True and store.task_baseline(TASK) == integrated
    assert events(store, "task.rebaselined")[0]["forced"] is True


def test_rebaseline_refuses_a_task_without_a_candidate(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    store.set_task_baseline(TASK, head(project))
    with pytest.raises(RecoveryRefusal) as refused:
        rebaseline_task(store, project, TASK, ref_of(project))
    assert refused.value.code == "no_candidate"


def test_rebaseline_cli(tmp_path: Path) -> None:
    project, store, old_base, integrated, sha = stale_baseline_task(tmp_path)
    ref = ref_of(project)
    store.close()
    code, out, _ = cli(project, "rebaseline-task", "--task", TASK, "--to", ref)
    assert code == 0 and "no longer in the task diff: web-v2/page.tsx" in out and "changed files: 3 -> 1" in out
    code, out, _ = cli(project, "rebaseline-task", "--task", TASK, "--to", ref, "--json")
    assert code == 2 and json.loads(out)["code"] == "not_stale"


# --- 2b. advance-baseline ----------------------------------------------------------------------------------------------------------


def stale_worktree_task(tmp_path: Path):
    """A task whose worktree was cut from the OLD tip and holds a candidate; main then moves on (what the survivor wave hit)."""
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    old_base = store.set_task_baseline(TASK, head(project))
    bind_task_contract(store, project, TASK, old_base)
    run_path = prepare_task_workspace(project, TASK)
    (run_path / "docs" / "a.md").write_text("changed by the task\n", encoding="utf-8")
    old_candidate = GitWorkspace(run_path).commit_all("the task's own change")
    store.add_candidate(TASK, old_candidate, "provider", durable_handoff=True)
    store.advance_task(TASK, Stage.VALIDATE)
    contract_for_candidate(store, TASK, old_candidate, project)
    assert Validator().validate(store, TASK, old_candidate, project) is EvidenceStatus.PASSED
    (project / "newer.txt").write_text("main moved on\n", encoding="utf-8")
    GitWorkspace(project).commit_all("main moved on")
    store.block_task(TASK)
    return project, store, old_base, head(project), old_candidate, run_path


def test_advance_baseline_refuses_active_claim_running_execution_and_integration(tmp_path: Path) -> None:
    project, store, old_base, _, _, _ = stale_worktree_task(tmp_path)
    claim = store.acquire_claim(TASK, "w1")
    with pytest.raises(RecoveryRefusal) as claimed:
        advance_baseline(store, project, TASK, yes=True)
    assert claimed.value.code == "active_claim"
    store.release_claim(claim)
    execution = store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.VALIDATION)
    with pytest.raises(RecoveryRefusal) as running:
        advance_baseline(store, project, TASK, yes=True)
    assert running.value.code == "running_execution"
    store.finish_execution(execution, "FAILED")
    store.advance_task(TASK, Stage.INTEGRATE)
    with pytest.raises(RecoveryRefusal) as integrating:
        advance_baseline(store, project, TASK, yes=True)
    assert integrating.value.code == "integrating"
    assert store.task_baseline(TASK) == old_base and not events(store, "task.baseline_advanced")


def test_advance_baseline_refuses_without_yes_and_changes_nothing(tmp_path: Path) -> None:
    project, store, old_base, _, old_candidate, run_path = stale_worktree_task(tmp_path)
    with pytest.raises(RecoveryRefusal) as refused:
        advance_baseline(store, project, TASK)
    assert refused.value.code == "confirmation_required" and "--yes" in str(refused.value)
    assert store.task_baseline(TASK) == old_base and GitWorkspace(run_path).head() == old_candidate
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED and not events(store, "task.baseline_advanced")


def test_advance_baseline_refuses_a_target_that_does_not_descend_from_the_baseline(tmp_path: Path) -> None:
    project, store, old_base, _, _, _ = stale_worktree_task(tmp_path)
    git = GitWorkspace(project)
    main_ref = ref_of(project)
    git.run("checkout", "-q", "-b", "side", old_base)
    (project / "side.txt").write_text("s\n", encoding="utf-8")
    git.commit_all("side branch commit")
    side_head = head(project)
    git.run("checkout", "-q", main_ref.removeprefix("refs/heads/"))
    store.conn.execute("UPDATE task_baselines SET baseline_sha=? WHERE task_id=?", (side_head, TASK))
    store.conn.commit()
    with pytest.raises(RecoveryRefusal) as refused:
        advance_baseline(store, project, TASK, main_ref, yes=True)
    assert refused.value.code == "baseline_not_ancestor" and store.task_baseline(TASK) == side_head
    with pytest.raises(RecoveryRefusal) as same:  # moving to where the baseline already is is a no-op, not a repair
        advance_baseline(store, project, TASK, side_head, yes=True)
    assert same.value.code == "already_at_target"


def test_advance_baseline_refuses_a_quarantined_worktree_until_it_is_reclaimed(tmp_path: Path) -> None:
    project, store, old_base, _, _, run_path = stale_worktree_task(tmp_path)
    record_audit(store, "autonomy.decision", {"task_id": TASK, "action": "FAIL_CLOSED_QUARANTINE", "condition": "EXTERNAL_WORKSPACE_MUTATION"})
    with pytest.raises(RecoveryRefusal) as refused:
        advance_baseline(store, project, TASK, yes=True)
    assert refused.value.code == "worktree_quarantined" and store.task_baseline(TASK) == old_base
    claim_workspace(store, TASK, run_path)  # re-claimed after the restore
    report = advance_baseline(store, project, TASK, yes=True)
    assert report["old_baseline"] == old_base
    assert load_ownership(store, TASK).expected_head == head(project)  # the guard expects the new base, so the reset is not a foreign mutation


def test_advance_baseline_moves_baseline_contract_and_worktree_and_restarts_implementation(tmp_path: Path) -> None:
    project, store, old_base, new_base, old_candidate, run_path = stale_worktree_task(tmp_path)
    (run_path / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

    report = advance_baseline(store, project, TASK, ref_of(project), reason="main moved past the frozen baseline", yes=True)

    assert (report["old_baseline"], report["new_baseline"]) == (old_base, new_base)
    assert store.task_baseline(TASK) == new_base and store.task_contract(TASK)["baseline_sha"] == new_base
    task = store.get_task(TASK)
    assert (task["stage"], task["status"]) == (Stage.IMPLEMENT, TaskStatus.OPEN) and report["unblocked"] is True
    (event,) = events(store, "task.baseline_advanced")
    assert event["old_baseline"] == old_base and event["new_baseline"] == new_base and event["task_id"] == TASK
    assert event["reason"] == "main moved past the frozen baseline" and event["operator"] and event["operator_action"] == "ADVANCE_BASELINE"
    assert event["superseded_candidates"] == [old_candidate]
    # the worktree is exactly on the new base and clean; nothing the task left behind is lost
    assert GitWorkspace(run_path).head() == new_base and not (run_path / "scratch.txt").exists()
    git = GitWorkspace(project)
    refs = set(event["preserved_refs"])
    candidate_ref = [r for r in refs if r.endswith(old_candidate[:12])]
    snapshots = [r for r in refs if not r.endswith(old_candidate[:12])]
    assert len(candidate_ref) == 1 and len(snapshots) == 1
    assert git.run("rev-parse", "--verify", f"{candidate_ref[0]}^{{commit}}").stdout.strip() == old_candidate
    assert git.run("show", f"{snapshots[0]}:scratch.txt").stdout == "uncommitted\n"
    # history is kept: the old candidate, its evidence and its binding are still there
    assert store.latest_candidate(TASK)["sha"] == old_candidate
    assert store.has_evidence(TASK, old_candidate, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    assert store.contract_binding(TASK, old_candidate)["baseline_sha"] == old_base


def test_after_advance_old_evidence_does_not_count_and_a_fresh_candidate_is_built_on_the_new_base(tmp_path: Path) -> None:
    project, store, _, new_base, old_candidate, run_path = stale_worktree_task(tmp_path)
    advance_baseline(store, project, TASK, yes=True)

    assert prepare_task_workspace(project, TASK) == run_path and GitWorkspace(run_path).head() == new_base  # the reused worktree is fresh
    (run_path / "docs" / "a.md").write_text("redone on the new base\n", encoding="utf-8")
    fresh = GitWorkspace(run_path).commit_all("fresh attempt")
    store.add_candidate(TASK, fresh, "provider", durable_handoff=True)

    assert fresh != old_candidate and GitWorkspace(project).run("merge-base", "--is-ancestor", new_base, fresh, check=False).returncode == 0
    assert not store.has_evidence(TASK, fresh, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    bound = contract_for_candidate(store, TASK, fresh, project)
    assert bound.baseline_sha == new_base  # not the old baseline, and not inherited from the superseded candidate's binding
    assert changed_files(project, fresh, bound.baseline_sha) == ["docs/a.md"]  # main's newer.txt is not blamed on the task
    assert Validator().validate(store, TASK, fresh, project) is EvidenceStatus.PASSED


def test_advance_baseline_creates_the_worktree_at_the_target_when_the_task_never_had_one(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    old_base = store.set_task_baseline(TASK, head(project))
    (project / "newer.txt").write_text("x\n", encoding="utf-8")
    GitWorkspace(project).commit_all("main moved on")
    report = advance_baseline(store, project, TASK, yes=True)
    assert report["preserved_refs"] == [] and store.task_baseline(TASK) == head(project) != old_base
    assert GitWorkspace(prepare_task_workspace(project, TASK)).head() == head(project)


def test_advance_baseline_refuses_unreadable_ownership_embedded_repos_and_non_checkouts_without_changing_anything(tmp_path: Path) -> None:
    from stagemesh.workspace_guard import LEDGER_FILE, _gitdir
    from stagemesh.workspaces import task_workspace

    project, store, old_base, _, old_candidate, run_path = stale_worktree_task(tmp_path)
    ledger = _gitdir(run_path) / LEDGER_FILE
    ledger.write_text("{not json", encoding="utf-8")
    with pytest.raises(RecoveryRefusal) as unreadable:
        advance_baseline(store, project, TASK, yes=True)
    assert unreadable.value.code == "worktree_unreadable"
    ledger.unlink()
    nested = run_path / "vendored"
    nested.mkdir()
    GitWorkspace(nested).init_if_needed()
    (nested / "x.txt").write_text("x\n", encoding="utf-8")
    GitWorkspace(nested).commit_all("nested")
    with pytest.raises(RecoveryRefusal) as embedded:
        advance_baseline(store, project, TASK, yes=True)
    assert embedded.value.code == "worktree_unpreservable"
    assert store.task_baseline(TASK) == old_base and GitWorkspace(run_path).head() == old_candidate and not events(store, "task.baseline_advanced")
    assert not GitWorkspace(project).run("for-each-ref", "refs/stagemesh/preserved/").stdout.strip()  # nothing was preserved or reset either

    (tmp_path / "other").mkdir()
    other_project, other_store = _setup(tmp_path / "other", DOCS_CONTRACT)
    other_store.set_task_baseline(TASK, head(other_project))
    (other_project / "newer.txt").write_text("x\n", encoding="utf-8")
    GitWorkspace(other_project).commit_all("main moved on")
    leftover = task_workspace(other_project, TASK)
    leftover.mkdir(parents=True)
    (leftover / "notes.txt").write_text("not a checkout\n", encoding="utf-8")
    with pytest.raises(RecoveryRefusal) as not_checkout:
        advance_baseline(other_store, other_project, TASK, yes=True)
    assert not_checkout.value.code == "worktree_not_a_checkout" and (leftover / "notes.txt").exists()


def test_advance_baseline_refuses_a_live_worktree_owner(tmp_path: Path) -> None:
    import os

    from stagemesh.workspace_guard import OWNER_FILE, _gitdir

    project, store, old_base, _, _, run_path = stale_worktree_task(tmp_path)
    (_gitdir(run_path) / OWNER_FILE).write_text(json.dumps({"token": "t", "task_id": TASK, "pid": os.getpid(), "execution_id": "e1"}), encoding="utf-8")
    with pytest.raises(RecoveryRefusal) as refused:
        advance_baseline(store, project, TASK, yes=True)
    assert refused.value.code == "worktree_in_use" and store.task_baseline(TASK) == old_base


def test_advance_baseline_reseals_a_stale_workspace_ledger_instead_of_leaving_the_next_run_to_fail(tmp_path: Path) -> None:
    from stagemesh.workspace_guard import LEDGER_FILE, _gitdir

    project, store, old_base, new_base, old_candidate, run_path = stale_worktree_task(tmp_path)
    ledger = _gitdir(run_path) / LEDGER_FILE
    ledger.write_text(json.dumps({"head": old_base, "dirty": [], "dirty_count": 0, "dirty_digest": "", "candidate": None}), encoding="utf-8")
    advance_baseline(store, project, TASK, yes=True)
    assert json.loads(ledger.read_text(encoding="utf-8"))["head"] == new_base == GitWorkspace(run_path).head()


def test_advance_baseline_records_recovery_evidence_and_moves_state_in_one_transaction(tmp_path: Path) -> None:
    project, store, old_base, new_base, old_candidate, run_path = stale_worktree_task(tmp_path)
    store.add_task_remediation(TASK, "VALIDATE", old_candidate)
    with pytest.raises(TypeError):  # an audit payload that cannot be written rolls the whole move back
        store.advance_task_baseline(TASK, new_base, {"task_id": TASK, "bad": object()})
    assert store.task_baseline(TASK) == old_base and store.task_contract(TASK)["baseline_sha"] == old_base
    assert (store.get_task(TASK)["stage"], store.get_task(TASK)["status"]) == (Stage.VALIDATE, TaskStatus.BLOCKED)
    assert store.task_remediation_count(TASK, "VALIDATE") == 1 and not events(store, "task.baseline_advanced")

    report = advance_baseline(store, project, TASK, yes=True)

    (event,) = events(store, "task.baseline_advanced")
    assert event["old_worktree_head"] == old_candidate and event["previous_latest_candidate"] == old_candidate
    assert event["previous_stage"] == "VALIDATE" and event["previous_status"] == "BLOCKED" and event["remediations_cleared"] == 1
    assert event["snapshot_ref"] is None or event["snapshot_ref"] in event["preserved_refs"]
    assert report["remediations_cleared"] == 1 and report["unblocked"] is True
    assert len(events(store, "task.advance")) >= 1 and store.task_remediation_count(TASK, "VALIDATE") == 0


def test_advance_baseline_cli(tmp_path: Path) -> None:
    project, store, _, new_base, _, _ = stale_worktree_task(tmp_path)
    store.close()
    code, _, err = cli(project, "advance-baseline", "--task", TASK)
    assert code == 2 and "confirmation_required" in err
    code, out, _ = cli(project, "advance-baseline", "--task", TASK, "--reason", "r", "--yes", "--json")
    data = json.loads(out)
    assert code == 0 and data["new_baseline"] == new_base and data["stage"] == "IMPLEMENT" and data["operator_action"] == "ADVANCE_BASELINE"
    code, out, _ = cli(project, "advance-baseline", "--task", TASK, "--yes", "--json")
    assert code == 2 and json.loads(out)["code"] == "already_at_target"


# --- 3. diagnosis: stale_baseline and repeated non-code failures stop before the provider is called again ---------------------------


def test_stale_baseline_is_diagnosed_and_auto_rebaselined_before_spending_a_remediation_attempt(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    store.set_task_baseline(TASK, head(project))
    (project / "web-v2").mkdir()
    (project / "web-v2" / "page.tsx").write_text("x\n", encoding="utf-8")
    GitWorkspace(project).commit_all("another task integrated")
    executor = Attempts(path="docs/a.md")  # the provider's work is fine; the baseline is what is wrong
    drive(project, store, executor)
    assert executor.calls == 1 and store.get_task(TASK)["status"] == TaskStatus.DONE
    assert store.task_remediation_count(TASK, "VALIDATE") == 0
    (rebaseline,) = events(store, "task.rebaselined")
    assert rebaseline["reason"] == "automatic stale baseline recovery"
    assert store.has_evidence(TASK, rebaseline["candidate_sha"], EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    assert not events(store, "task.diagnosis_stop")
    assert not events(store, "task.remediation_exhausted")
    diagnosis = diagnose(store, TASK, project)
    assert diagnosis is None


def test_stale_baseline_is_recovered_without_operator_rebaseline_or_sqlite_edit(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    store.set_task_baseline(TASK, head(project))
    (project / "web-v2").mkdir()
    (project / "web-v2" / "page.tsx").write_text("x\n", encoding="utf-8")
    GitWorkspace(project).commit_all("another task integrated")
    drive(project, store, Attempts(path="docs/a.md"))
    store.close()
    code, out, _ = cli(project, "task-doctor", "--task", TASK, "--json")
    doctor = json.loads(out)
    assert code == 0
    assert (doctor["stage"], doctor["status"]) == ("DONE", "DONE") and doctor["baseline"]["stale"] is False
    assert doctor["recommended_command"] == ""


def test_continue_recovers_previously_blocked_stale_baseline_without_operator_command(tmp_path: Path) -> None:
    project, store, _, _, sha = stale_baseline_task(tmp_path)
    assert Validator().validate(store, TASK, sha, project) is EvidenceStatus.FAILED
    store.block_task(TASK)
    store.close()

    code, out, _ = cli(project, "continue", "--dry-run", "--json", "--task", TASK)
    result = json.loads(out)

    assert code == 0 and result["stop_reason"] == "DONE"
    assert result["recovered"][0]["reason"] == "automatic stale baseline recovery"
    assert "rebaseline-task" not in json.dumps(result)


def test_repeated_validation_gate_failures_stop_without_calling_the_provider_again(tmp_path: Path) -> None:
    broken = {**DOCS_CONTRACT, "required_tests": [{"name": "unit", "command": ["stagemesh-no-such-tool-xyz", "--run"]}]}
    project, store = _setup(tmp_path, broken)
    executor = Attempts(path="docs/a.md")
    drive(project, store, executor)
    assert executor.calls == 2 and store.get_task(TASK)["status"] == TaskStatus.BLOCKED  # the budget of 3 was not spent
    (stop,) = events(store, "task.diagnosis_stop")
    assert stop["category"] == VALIDATION_GATE and stop["repeated"]
    assert f"stagemesh rebind-contract --task {TASK} --validate" in stop["recommendation"]


def test_repeated_provider_no_progress_is_not_retried(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    executor = Attempts(nothing=True)
    drive(project, store, executor, ticks=10)
    assert executor.calls == 3 and store.get_task(TASK)["status"] == TaskStatus.BLOCKED  # 2 no-progress attempts + one clean recovery round
    stop = events(store, "task.diagnosis_stop")[-1]
    assert stop["category"] == "provider_no_progress" and "stagemesh continue" in stop["recommendation"]


def test_a_repair_command_starts_a_fresh_failure_history(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    drive(project, store, Attempts(path="src/app.py"))  # contract_scope, repeated, blocked
    assert diagnose(store, TASK, project).repeated
    write_contract(project, {**DOCS_CONTRACT, "allowed_files": ["docs/**", "src/**"]})
    rebind_contract(store, project, TASK)
    assert diagnose(store, TASK, project) is None  # earlier failures are history, not a repeat
    assert store.get_task(TASK)["status"] == TaskStatus.OPEN


# --- 5. review findings: stored exactly, shown, and propagated verbatim -------------------------------------------------------------

LONG_FINDING = ("The retry loop in worker.py never backs off, so a failing dependency is hammered. " + "Detail line. " * 60).strip()


def review_failed(store, sha: str) -> None:
    store.add_candidate(TASK, sha, "impl", durable_handoff=True)
    store.add_evidence(TASK, sha, EvidenceKind.REVIEW, EvidenceStatus.FAILED, {"findings": [], "finding_count": 2})
    store.upsert_finding(finding_identity(sha, LONG_FINDING), TASK, sha, "error", LONG_FINDING)
    store.upsert_finding(finding_identity(sha, "Missing test for the timeout path"), TASK, sha, "warning", "Missing test for the timeout path")


def test_remediation_prompt_includes_exact_review_findings_and_candidate_sha(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    sha = "a" * 40
    review_failed(store, sha)
    store.add_task_remediation(TASK, "REVIEW", sha)
    prompt = _remediation_prompt(remediation_context(store, TASK, project))
    assert sha in prompt
    assert LONG_FINDING in prompt  # verbatim, not truncated to a few hundred characters
    assert "[warning] Missing test for the timeout path" in prompt and "[error] The retry loop" in prompt


def test_review_findings_are_shown_in_diagnose_doctor_and_run_summary(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    sha = "b" * 40
    review_failed(store, sha)
    diagnosis = diagnose(store, TASK, project)
    assert [f["message"] for f in diagnosis.review_findings] == [LONG_FINDING, "Missing test for the timeout path"]
    assert all(f["candidate_sha"] == sha and f["source"] == "REVIEW" for f in diagnosis.review_findings)
    assert LONG_FINDING.splitlines()[0] in "\n".join(diagnosis.format_lines())

    summary = RunSummary(False, "BLOCKED", task_id=TASK)
    summary.detail["diagnosis"] = diagnosis.to_dict()
    assert "[warning] Missing test for the timeout path" in format_stop(summary)

    report = task_doctor(store, project, TASK)
    assert [f["message"] for f in report["review_findings"]] == [LONG_FINDING, "Missing test for the timeout path"]
    store.close()
    code, out, _ = cli(project, "diagnose", "--task", TASK)
    assert code == 0 and f"findings on candidate {sha[:12]}" in out and "[warning] Missing test for the timeout path" in out
    code, out, _ = cli(project, "diagnose", "--task", TASK, "--json")
    assert [f["message"] for f in json.loads(out)["review_findings"]][1] == "Missing test for the timeout path"


def test_a_real_remediation_attempt_prompt_carries_the_findings(tmp_path: Path) -> None:
    """End to end through the coordinator: a failed validation queues remediation and the next prompt has the exact finding."""
    project, store = _setup(tmp_path, {**DOCS_CONTRACT, "allowed_files": ["docs/**"]})
    executor = Attempts(path="src/app.py")
    coordinator = Coordinator(store, project, executor=executor, target=TargetSelection(TASK))
    for _ in range(4):  # PLAN? -> IMPLEMENT -> VALIDATE(fail) -> remediation queued
        coordinator.tick()
        if store.latest_task_remediation(TASK) is not None:
            break
    context = remediation_context(store, TASK, project)
    sha = str(store.latest_candidate(TASK)["sha"])
    prompt = _remediation_prompt(context)
    assert sha in prompt and "src/app.py" in prompt
    assert '"integration_ref":' in prompt and '"integration_sha":' in prompt


def test_cleared_remediation_is_not_used_as_the_next_worker_context(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    sha = "c" * 40
    store.add_candidate(TASK, sha, "impl", durable_handoff=True)
    store.upsert_finding(finding_identity(sha, "broken"), TASK, sha, "error", "broken")
    store.add_task_remediation(TASK, "VALIDATE", sha)

    assert remediation_context(store, TASK, project) is not None

    assert store.clear_task_remediations(TASK, "VALIDATE") == 1
    assert remediation_context(store, TASK, project) is None


def test_owner_state_next_action_waits_when_human_judgment_is_required(tmp_path: Path) -> None:
    from stagemesh.task_completion import task_owner_state

    project, store = _setup(tmp_path, DOCS_CONTRACT)
    sha = "d" * 40
    message = "Need operator decision before changing the public contract"
    store.add_candidate(TASK, sha, "impl", durable_handoff=True)
    store.advance_task(TASK, Stage.VALIDATE)
    store.add_evidence(TASK, sha, EvidenceKind.VALIDATION, EvidenceStatus.FAILED, {"findings": [{"message": message}]})
    store.upsert_finding(finding_identity(sha, message), TASK, sha, "error", message)

    state = task_owner_state(store, TASK, project)

    assert state["human_decision_required"] is True
    assert state["next_action"] == "await_human_decision"


# --- 6. task-doctor --------------------------------------------------------------------------------------------------------------------


def audit_count(store) -> int:
    return store.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]


def test_task_doctor_reports_stale_baseline_and_is_read_only(tmp_path: Path) -> None:
    project, store, old_base, integrated, sha = stale_baseline_task(tmp_path)
    Validator().validate(store, TASK, sha, project)
    before = (audit_count(store), store.get_task(TASK)["stage"], store.task_baseline(TASK))
    report = task_doctor(store, project, TASK)
    assert (audit_count(store), store.get_task(TASK)["stage"], store.task_baseline(TASK)) == before  # nothing written
    assert report["stage"] == "VALIDATE" and report["status"] == "OPEN"
    assert report["latest_candidate"]["sha"] == sha and report["baseline"]["sha"] == old_base
    assert report["baseline"]["stale"] is True and report["baseline"]["proposed_sha"] == integrated
    assert report["contract"]["version_supported"] and report["contract"]["current"]["version"] == 1
    assert {f["path"] for f in report["validation_failures"]} >= {"web-v2/page.tsx"}
    assert report["diagnosis"]["category"] == STALE_BASELINE
    assert report["recommended_command"] == f"stagemesh rebaseline-task --task {TASK} --to {ref_of(project)}"
    assert report["active_claim"] is None and report["active_executions"] == []


def test_task_doctor_text_and_json_output(tmp_path: Path) -> None:
    project, store, old_base, integrated, sha = stale_baseline_task(tmp_path)
    Validator().validate(store, TASK, sha, project)
    store.close()
    code, out, _ = cli(project, "task-doctor", "--task", TASK, "--json")
    data = json.loads(out)
    assert code == 0 and data["task_id"] == TASK and data["baseline"]["stale"] and data["diagnosis"]["category"] == "stale_baseline"
    for key in ("stage", "status", "active_claim", "active_executions", "latest_candidate", "baseline", "contract", "validation_failures", "review_findings", "diagnosis", "recommended_command"):
        assert key in data
    code, text, _ = cli(project, "task-doctor", "--task", TASK)
    assert code == 0
    assert f"task {TASK}" in text and "stage/status: VALIDATE/OPEN" in text and f"latest candidate: {sha}" in text
    assert f"baseline: {old_base}" in text and "stale vs" in text and "web-v2/page.tsx" in text
    assert "contract: digest" in text and "version 1" in text and "[outside_allowed_files]" in text
    assert "diagnosis: stale_baseline" in text and f"next: stagemesh rebaseline-task --task {TASK}" in text
    assert cli(project, "task-doctor", "--task", "nope")[0] == 2


def test_task_doctor_flags_an_unsupported_stored_version_instead_of_needing_sqlite(tmp_path: Path) -> None:
    project, store, _, sha = frozen_task_with_candidate(tmp_path)
    store.conn.execute("UPDATE task_contracts SET version=2 WHERE task_id=?", (TASK,))
    store.conn.commit()
    report = task_doctor(store, project, TASK)
    assert report["contract"]["version_supported"] is False and report["contract"]["frozen"]["version"] == 2
    assert report["recommended_command"] == f"stagemesh rebind-contract --task {TASK} --validate"
    store.close()
    code, text, _ = cli(project, "task-doctor", "--task", TASK)
    assert "UNSUPPORTED; supported 1" in text and "rebind-contract" in text


def test_task_doctor_shows_active_claim_and_contract_file_drift(tmp_path: Path) -> None:
    project, store, _, _ = frozen_task_with_candidate(tmp_path)
    store.acquire_claim(TASK, "worker-9")
    write_contract(project, {**DOCS_CONTRACT, "allowed_files": ["docs/**", "lib/**"]})
    report = task_doctor(store, project, TASK)
    assert report["active_claim"]["worker_id"] == "worker-9" and report["contract"]["file_matches_frozen"] is False
    assert report["recommended_command"].startswith("stagemesh recover-stale")
    store.release_claim(report["active_claim"]["id"])
    assert task_doctor(store, project, TASK)["recommended_command"] == f"stagemesh rebind-contract --task {TASK} --validate"
