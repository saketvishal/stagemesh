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
from stagemesh.contracts import CONTRACT_VERSION, ContractError
from stagemesh.coordinator import Coordinator, TargetSelection
from stagemesh.diagnosis import STALE_BASELINE, VALIDATION_GATE, diagnose
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, Stage, TaskStatus
from stagemesh.git import GitWorkspace
from stagemesh.providers import _remediation_prompt
from stagemesh.recovery import RecoveryRefusal, rebaseline_task, rebind_contract, task_doctor
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


# --- 3. diagnosis: stale_baseline and repeated non-code failures stop before the provider is called again ---------------------------


def test_stale_baseline_is_diagnosed_and_stops_before_spending_a_remediation_attempt(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    store.set_task_baseline(TASK, head(project))
    (project / "web-v2").mkdir()
    (project / "web-v2" / "page.tsx").write_text("x\n", encoding="utf-8")
    GitWorkspace(project).commit_all("another task integrated")
    executor = Attempts(path="docs/a.md")  # the provider's work is fine; the baseline is what is wrong
    drive(project, store, executor)
    assert executor.calls == 1 and store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert store.task_remediation_count(TASK, "VALIDATE") == 0
    (stop,) = events(store, "task.diagnosis_stop")
    assert stop["category"] == STALE_BASELINE and "rebaseline-task" in stop["recommendation"] and TASK in stop["recommendation"]
    assert events(store, "task.remediation_exhausted")[-1]["reason"] == "stale_baseline_diagnosed"
    diagnosis = diagnose(store, TASK, project)
    assert diagnosis.category == STALE_BASELINE and diagnosis.stops_remediation and diagnosis.baseline["unrelated_files"] == ["web-v2/page.tsx"]


def test_full_recovery_of_the_stale_baseline_stop_without_touching_sqlite(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    store.set_task_baseline(TASK, head(project))
    (project / "web-v2").mkdir()
    (project / "web-v2" / "page.tsx").write_text("x\n", encoding="utf-8")
    GitWorkspace(project).commit_all("another task integrated")
    drive(project, store, Attempts(path="docs/a.md"))
    store.close()
    code, out, _ = cli(project, "task-doctor", "--task", TASK, "--json")
    assert json.loads(out)["recommended_command"] == f"stagemesh rebaseline-task --task {TASK} --to {ref_of(project)}"
    assert cli(project, "rebaseline-task", "--task", TASK, "--to", ref_of(project), "--validate")[0] == 0
    code, out, _ = cli(project, "task-doctor", "--task", TASK, "--json")
    doctor = json.loads(out)
    assert (doctor["stage"], doctor["status"]) == ("REVIEW", "OPEN") and doctor["baseline"]["stale"] is False


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
    assert executor.calls == 2 and store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    (stop,) = events(store, "task.diagnosis_stop")
    assert stop["category"] == "provider_no_progress" and "task-doctor" in stop["recommendation"]


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
    prompt = _remediation_prompt(remediation_context(store, TASK))
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
    context = remediation_context(store, TASK)
    sha = str(store.latest_candidate(TASK)["sha"])
    prompt = _remediation_prompt(context)
    assert sha in prompt and "src/app.py" in prompt


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
