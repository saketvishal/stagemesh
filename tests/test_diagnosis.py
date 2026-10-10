from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.coordinator import Coordinator, TargetSelection
from stagemesh.diagnosis import (
    CONTRACT_SCOPE,
    IMPLEMENTATION_DEFECT,
    INTEGRATION_CONFLICT,
    PROVIDER_NO_PROGRESS,
    REVIEW_FINDING,
    VALIDATION_GATE,
    DiagnosisPolicy,
    diagnose,
    normalize,
    parse_analysis,
)
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus, Stage, TaskStatus
from stagemesh.execution import ExecutionResult, Executor
from stagemesh.git import GitWorkspace
from stagemesh.providers import _remediation_prompt
from stagemesh.remediation import finding_identity, remediation_context
from stagemesh.run_ready import RunSummary, drive_task, format_stop
from stagemesh.workspaces import prepare_task_workspace, record_task_baseline

from test_bounded_execution import TASK, _setup

PY = sys.executable
DOCS_CONTRACT = {
    "objective": "docs only",
    "allowed_files": ["docs/**"],
    "required_tests": [{"name": "smoke", "command": [PY, "-c", "pass"]}],
}


WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")


class Attempts(Executor):
    """Each call commits a different change, optionally outside the contract, so every attempt is a new candidate."""

    name = "attempts"

    def __init__(self, path: str = "docs/a.md", nothing: bool = False):
        self.path, self.nothing, self.calls = path, nothing, 0

    def run(self, store, task_id, claim_id, project: Path) -> ExecutionResult:
        self.calls += 1
        execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION)
        run_path = prepare_task_workspace(project, task_id)
        record_task_baseline(store, task_id, run_path)
        if self.nothing:
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason="no_implementation_change")
        target = run_path / self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{WORDS[self.calls % len(WORDS)]}\n", encoding="utf-8")
        sha = GitWorkspace(run_path).commit_all(f"attempt {self.calls}")
        store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)


def drive(project: Path, store, executor: Executor, policy: DiagnosisPolicy | None = None, ticks: int = 40) -> Coordinator:
    coordinator = Coordinator(store, project, executor=executor, target=TargetSelection(TASK), diagnosis_policy=policy)
    for _ in range(ticks):
        coordinator.tick()
        if store.get_task(TASK)["status"] in (TaskStatus.BLOCKED, TaskStatus.DONE):
            break
    return coordinator


def events(store, event_type: str) -> list[dict]:
    rows = store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (event_type,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def failed_validation(store, sha: str, findings: list[dict], gates: list[dict] | None = None, task: str = TASK) -> None:
    store.add_candidate(task, sha, "p", durable_handoff=True)
    store.add_evidence(task, sha, EvidenceKind.VALIDATION, EvidenceStatus.FAILED, {"findings": findings, "gates": gates or []})
    for finding in findings:
        store.upsert_finding(finding_identity(sha, finding["message"]), task, sha, "error", finding["message"])


def gate_finding(name: str, output: str, returncode: int | None = 1) -> dict:
    return {"severity": "error", "code": "gate_failed", "message": f"{name} failed", "returncode": returncode, "stdout": output, "stderr": ""}


# --- a repeated failure stops early with a diagnosis ---------------------------------------------------------------------


def test_repeated_contract_scope_failure_stops_before_the_budget_is_spent(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    executor = Attempts(path="src/app.py")  # always outside docs/**
    drive(project, store, executor)
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert executor.calls == 2  # the third attempt (the normal budget is 3) was never spent
    (stop,) = events(store, "task.diagnosis_stop")
    assert stop["category"] == CONTRACT_SCOPE and stop["repeated"] and stop["repeat_count"] == 2 and stop["stage"] == "VALIDATE"
    assert "outside_allowed_files" in stop["codes"] and "allowed_files" in stop["recommendation"]
    assert len(stop["compared_candidates"]) == 2
    assert events(store, "task.remediation_exhausted")[-1]["reason"] == "repeated_failure_diagnosed"


def test_run_summary_reports_the_diagnosis_instead_of_a_generic_block(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    coordinator = Coordinator(store, project, executor=Attempts(path="src/app.py"), target=TargetSelection(TASK))
    summary = RunSummary(True, "UNSET", task_id=TASK)
    drive_task(store, project, coordinator, TASK, summary, max_steps=40)
    assert summary.stop_reason == "BLOCKED" and summary.detail["stopped_early"] is True
    assert summary.message.startswith("stopped early, contract_scope (2 identical failures)")
    diagnosis = summary.detail["diagnosis"]
    assert diagnosis["repeated"] and [c["same_as_previous"] for c in diagnosis["comparison"]] == [False, True]
    text = format_stop(summary)
    assert "diagnosis: contract_scope at VALIDATE" in text and "stopped early" in text


def test_repeated_gate_failure_on_the_code_is_an_implementation_defect(tmp_path: Path) -> None:
    contract = dict(DOCS_CONTRACT, required_tests=[{"name": "docs-static-unit", "command": [PY, "-c", "import sys; print('FAILED test_x - assert 1 == 2'); sys.exit(1)"]}])
    project, store = _setup(tmp_path, contract)
    executor = Attempts()
    drive(project, store, executor)
    (stop,) = events(store, "task.diagnosis_stop")
    assert stop["category"] == IMPLEMENTATION_DEFECT and stop["failed_gates"] == ["docs-static-unit"] and executor.calls == 2

def test_diagnose_and_stop_output_include_gate_excerpt(tmp_path: Path) -> None:
    contract = dict(
        DOCS_CONTRACT,
        required_tests=[{"name": "docs-static-unit", "command": [PY, "-c", "import sys; print('FAILED test_x - assert 1 == 2'); sys.exit(1)"]}],
    )
    project, store = _setup(tmp_path, contract)
    coordinator = Coordinator(store, project, executor=Attempts(), target=TargetSelection(TASK))
    summary = RunSummary(True, "UNSET", task_id=TASK)

    drive_task(store, project, coordinator, TASK, summary, max_steps=40)

    diagnosis = diagnose(store, TASK)
    assert diagnosis and "FAILED test_x" in diagnosis.failing_evidence[-1]["excerpt"]
    assert "output excerpt:" in "\n".join(diagnosis.format_lines())
    stop_text = format_stop(summary)
    assert "output excerpt:" in stop_text and "FAILED test_x" in stop_text


def test_a_different_failure_each_time_is_progress_not_a_repeat(tmp_path: Path) -> None:
    script = (
        "import pathlib, sys\n"
        "w = pathlib.Path('docs/a.md').read_text().strip()\n"
        "print('FAILED test_' + w + '_case assertion error for ' + w); sys.exit(1)\n"
    )
    contract = dict(DOCS_CONTRACT, required_tests=[{"name": "docs-static-unit", "command": [PY, "-c", script]}])
    project, store = _setup(tmp_path, contract)
    executor = Attempts()
    drive(project, store, executor)
    assert executor.calls == 4 and not events(store, "task.diagnosis_stop")  # distinct failures keep the normal remediation loop
    assert all(d["repeated"] is False for d in events(store, "task.diagnosis"))
    assert diagnose(store, TASK).categories == {IMPLEMENTATION_DEFECT: 4}


def test_stop_on_repeat_can_be_switched_off(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    executor = Attempts(path="src/app.py")
    drive(project, store, executor, DiagnosisPolicy(stop_on_repeat=False))
    assert executor.calls == 4  # the full budget (3 remediations) is spent, as before
    assert not events(store, "task.diagnosis_stop") and all(e["repeated"] or e["repeat_count"] == 1 for e in events(store, "task.diagnosis"))


# --- classification and comparison ---------------------------------------------------------------------------------------


def test_classification_of_each_failure_kind(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    failed_validation(store, "a" * 40, [{"severity": "error", "code": "forbidden_file_changed", "message": "x forbidden", "path": "secrets/k"}])
    assert diagnose(store, TASK).category == CONTRACT_SCOPE and diagnose(store, TASK).failing_evidence[-1]["paths"] == ["secrets/k"]
    failed_validation(store, "b" * 40, [{"severity": "error", "code": "planned_check_missing_command", "message": "planned check has no command"}])
    assert diagnose(store, TASK).category == VALIDATION_GATE
    failed_validation(store, "c" * 40, [gate_finding("lint", "bash: ruff: command not found", returncode=127)], [{"name": "lint", "status": "FAILED"}])
    assert diagnose(store, TASK).category == VALIDATION_GATE  # the tool is missing: not a code defect
    failed_validation(store, "d" * 40, [gate_finding("unit", "FAILED tests/test_a.py::test_b - AssertionError")], [{"name": "unit", "status": "FAILED"}])
    assert diagnose(store, TASK).category == IMPLEMENTATION_DEFECT
    store.add_evidence(TASK, "d" * 40, EvidenceKind.INTEGRATION, EvidenceStatus.FAILED, {"findings": [{"code": "integration_rebase_conflict", "message": "conflict in a.py"}]})
    assert diagnose(store, TASK).category == INTEGRATION_CONFLICT


def test_review_finding_repeats_are_matched_despite_rewording(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    for sha, message in (
        ("a" * 40, "src/x.py:10 acquires a provider slot but ignores the returned value when the provider is cooling down"),
        ("b" * 40, "src/x.py:12 acquires a provider slot, ignoring the value returned while the provider is cooling down"),
    ):
        store.add_candidate(TASK, sha, "p", durable_handoff=True)
        store.add_evidence(TASK, sha, EvidenceKind.REVIEW, EvidenceStatus.FAILED, {"findings": []})
        store.upsert_finding(finding_identity(sha, message), TASK, sha, "error", message)
    diagnosis = diagnose(store, TASK)
    assert diagnosis.category == REVIEW_FINDING and diagnosis.repeated and "provider slot" in diagnosis.recommendation
    other = "c" * 40  # an unrelated concern is not a repeat
    store.add_candidate(TASK, other, "p", durable_handoff=True)
    store.add_evidence(TASK, other, EvidenceKind.REVIEW, EvidenceStatus.FAILED, {"findings": []})
    store.upsert_finding(finding_identity(other, "docs typo in README heading"), TASK, other, "error", "docs typo in README heading")
    assert diagnose(store, TASK).repeated is False


def test_comparison_across_candidates_and_normalization(tmp_path: Path) -> None:
    assert normalize("took 3.2s in C:\\Users\\x\\AppData\\Local\\Temp\\pytest-of-x\\t1 at 1f3410706e55") == normalize("took 9.9s in C:\\Users\\y\\AppData\\Local\\Temp\\pytest-of-y\\t7 at abcdef123456")
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    for sha, out in (("a" * 40, "FAILED test_a in 1.2s"), ("b" * 40, "FAILED test_a in 8.7s"), ("c" * 40, "FAILED test_zzz completely different")):
        failed_validation(store, sha, [gate_finding("unit", out)], [{"name": "unit", "status": "FAILED"}])
    diagnosis = diagnose(store, TASK)
    assert [c["same_as_previous"] for c in diagnosis.comparison] == [False, True, False]
    assert not diagnosis.repeated and diagnosis.categories == {IMPLEMENTATION_DEFECT: 3}
    assert diagnose(store, TASK, threshold=2).repeat_count == 1


def test_provider_no_progress_from_failed_attempts_and_identical_trees(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    executor = Attempts(nothing=True)
    coordinator = Coordinator(store, project, executor=executor, target=TargetSelection(TASK))
    for _ in range(2):
        coordinator.tick()
    diagnosis = diagnose(store, TASK, project)
    assert diagnosis.category == PROVIDER_NO_PROGRESS and diagnosis.repeated and diagnosis.no_progress["attempts"] == 2
    assert "stagemesh continue" in diagnosis.recommendation
    assert "try a different provider" not in diagnosis.recommendation
    (tmp_path / "t2").mkdir()
    project2, store2 = _setup(tmp_path / "t2", DOCS_CONTRACT)
    run_path = prepare_task_workspace(project2, TASK)
    git = GitWorkspace(run_path)
    (run_path / "docs" / "a.md").write_text("same\n", encoding="utf-8")
    first = git.commit_all("one")
    git.run("commit", "--amend", "-m", "two", "--allow-empty")  # a new commit with the identical tree
    second = git.head()
    assert first != second
    for sha in (first, second):
        failed_validation(store2, sha, [gate_finding("unit", "FAILED same")], [{"name": "unit", "status": "FAILED"}])
    twin = diagnose(store2, TASK, project2)
    assert twin.category == PROVIDER_NO_PROGRESS and twin.no_progress["identical_trees"]


def test_no_failures_means_no_diagnosis(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    assert diagnose(store, TASK, project) is None


# --- optional diagnostic provider pass -----------------------------------------------------------------------------------


def _analyst(calls: list, answer: dict | None = None, boom: bool = False):
    def analyst(diagnosis, sha):
        calls.append((diagnosis.category, diagnosis.repeated, sha))
        if boom:
            raise RuntimeError("provider exploded")
        return answer if answer is not None else {"provider": "claude", "text": "root cause: the contract forbids src/", "category": "contract_scope"}

    return analyst


def test_analyst_runs_before_another_attempt_and_its_answer_reaches_the_next_prompt(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    calls: list = []
    drive(project, store, Attempts(path="src/app.py"), DiagnosisPolicy(dispatch="every_failure", analyst=_analyst(calls)), ticks=6)
    assert len(calls) >= 1 and calls[0][1] is False  # asked after the FIRST failure, before attempt two was spent
    first = events(store, "task.diagnosis")[0]
    assert first["provider"] == "claude" and "contract forbids" in first["provider_analysis"]
    context = remediation_context(store, TASK)
    assert context["diagnosis"]["category"] == CONTRACT_SCOPE and "contract forbids" in context["diagnosis"]["provider_analysis"]
    prompt = _remediation_prompt(context)
    assert "Diagnosis (contract_scope)" in prompt and "Independent analysis: root cause: the contract forbids src/" in prompt


def test_dispatch_modes_and_a_failing_analyst_never_break_the_lifecycle(tmp_path: Path) -> None:
    for mode, expected_min, expected_max in (("never", 0, 0), ("on_repeat", 1, 1), ("every_failure", 2, 2)):
        (tmp_path / mode).mkdir()
        project, store = _setup(tmp_path / mode, DOCS_CONTRACT)
        calls: list = []
        drive(project, store, Attempts(path="src/app.py"), DiagnosisPolicy(dispatch=mode, analyst=_analyst(calls)))
        assert expected_min <= len(calls) <= expected_max, (mode, calls)
        if mode == "on_repeat":
            assert calls[0][1] is True  # only asked once the failure repeated
    (tmp_path / "boom").mkdir()
    project, store = _setup(tmp_path / "boom", DOCS_CONTRACT)
    drive(project, store, Attempts(path="src/app.py"), DiagnosisPolicy(analyst=_analyst([], boom=True)))
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED  # still diagnosed and stopped
    assert events(store, "task.diagnosis_provider_failed") and events(store, "task.diagnosis_stop")[0]["provider_analysis"] is None


def test_parse_analysis_ignores_provider_failures() -> None:
    assert parse_analysis(json.dumps({"decision": "INFRASTRUCTURE_FAILURE", "reason": "quota"}), "codex") is None
    assert parse_analysis(json.dumps({"decision": "FAIL", "findings": []}), "codex") is None  # the read-only run mutated something
    assert parse_analysis("", "codex") is None
    parsed = parse_analysis(json.dumps({"root_cause": "r", "next_step": "n", "category": "contract_scope"}), "codex")
    assert parsed == {"provider": "codex", "text": "r; n", "category": "contract_scope"}
    assert parse_analysis("plain words", "codex")["text"] == "plain words"


# --- CLI and config ------------------------------------------------------------------------------------------------------


def _cli(project: Path, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), *argv])
    return code, out.getvalue()


def test_diagnose_command_reports_human_and_json(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    drive(project, store, Attempts(path="src/app.py"))
    store.close()
    code, out = _cli(project, "diagnose", "--task", TASK, "--json")
    data = json.loads(out)
    assert code == 0 and data["category"] == CONTRACT_SCOPE and data["repeated"] and len(data["comparison"]) == 2
    code, text = _cli(project, "diagnose", "--task", TASK)
    assert code == 0 and "diagnosis: contract_scope at VALIDATE (repeated 2x)" in text and "next step:" in text
    assert _cli(project, "diagnose", "--task", "nope")[0] == 2


def test_diagnose_command_with_nothing_to_diagnose(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    store.close()
    code, out = _cli(project, "diagnose", "--task", TASK, "--json")
    assert code == 0 and json.loads(out) == {"task_id": TASK, "diagnosis": None}


def test_diagnosis_config_validation(tmp_path: Path) -> None:
    project = tmp_path / "p"
    (project / ".stagemesh").mkdir(parents=True)
    config_path = project / ".stagemesh" / "config.json"
    config_path.write_text(json.dumps({"diagnosis": {"repeat_threshold": 3, "stop_on_repeat": False, "provider": "claude", "dispatch": "on_repeat"}}), encoding="utf-8")
    diagnosis = load_config(project).diagnosis
    assert (diagnosis.repeat_threshold, diagnosis.stop_on_repeat, diagnosis.provider, diagnosis.dispatch) == (3, False, "claude", "on_repeat")
    (tmp_path / "plain").mkdir()
    assert load_config(tmp_path / "plain").diagnosis.provider is None
    for bad in ({"repeat_threshold": 1}, {"stop_on_repeat": "yes"}, {"provider": "no-such"}, {"dispatch": "sometimes"}, {"extra": 1}):
        config_path.write_text(json.dumps({"diagnosis": bad}), encoding="utf-8")
        with pytest.raises(ConfigValidationError):
            load_config(project)
    with pytest.raises(ValueError):
        DiagnosisPolicy(repeat_threshold=1)


def test_retry_task_resets_what_counts_as_a_repeat(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    executor = Attempts(path="src/app.py")
    coordinator = drive(project, store, executor)
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED and diagnose(store, TASK).repeated
    before = len(events(store, "task.diagnosis"))
    store.unblock_task(TASK)  # what `retry-task` does: a fresh budget
    fresh = diagnose(store, TASK)
    assert fresh is None or not fresh.repeated  # earlier failures are history, not a repeat against the operator's retry
    for _ in range(10):
        coordinator.tick()
        if store.get_task(TASK)["status"] == TaskStatus.BLOCKED:
            break
    after = events(store, "task.diagnosis")[before:]
    assert after[0]["repeat_count"] == 1 and after[0]["repeated"] is False  # the re-run failure starts a fresh comparison
    assert after[-1]["repeated"] is True and executor.calls == 3  # one more attempt, then it stops again
    assert len(events(store, "task.diagnosis_stop")) == 2
