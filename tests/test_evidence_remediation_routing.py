from __future__ import annotations

import json
import sys
from pathlib import Path

from stagemesh.contract_binding import contract_for_candidate
from stagemesh.contracts import canonical_contract_json, evaluate_contract, parse_contract
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, Stage, TaskStatus
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.remediation import finding_identity
from stagemesh.review_remediation import (
    EXTERNAL_EVIDENCE,
    HUMAN_DECISION,
    VALIDATION,
    classify_review_finding,
    review_remediations,
)
from stagemesh.validation import (
    contract_with_current_profile_gates,
    current_validation_status,
)

TASK = "138"
CANDIDATE = "2" * 40
OLD_GATE = {"name": "project-acceptance", "command": [sys.executable, "-c", "pass"], "timeout_seconds": 60}
CURRENT_GATE = {
    "name": "project-acceptance",
    "command": [sys.executable, "-c", "print('current')"],
    "timeout_seconds": 60,
}


def _project(tmp_path: Path, *, generated: bool = True) -> tuple[Path, Store, object]:
    project = tmp_path / "repo"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "Test")
    (project / "README.md").write_text("base\n", encoding="utf-8")
    git.commit_all("base")
    runtime = project / ".stagemesh"
    (runtime / "contracts").mkdir(parents=True)
    contract = {
        "objective": "Implement the mobile flow and run the project checks.",
        "explicit": True,
        "allowed_files": ["README.md"],
        "required_tests": [OLD_GATE],
        **({"generated_by": "stagemesh-auto-plan", "source_task": TASK} if generated else {}),
    }
    (runtime / "contracts" / f"{TASK}.json").write_text(json.dumps(contract), encoding="utf-8")
    if generated:
        profile = {
            "schema_version": 1,
            "contract_generation": "automatic",
            "name": "test",
            "forbidden_files": [".stagemesh/**", ".git/**"],
            "defaults": {"max_changed_files": 60, "max_diff_lines": 6000},
            "gates": {"current": CURRENT_GATE},
            "task_types": {
                "default": {
                    "summary": "test",
                    "allowed_files": ["**"],
                    "validation_level": "full",
                    "gates": ["current"],
                }
            },
            "type_selection": {"default_type": "default", "escalation_type": "default"},
        }
        (runtime / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    baseline = git.commit_all("test configuration")
    store = Store(runtime / "stagemesh.sqlite3")
    store.migrate()
    task_id = store.upsert_task("Mobile flow", source="github", source_id=TASK)
    assert task_id == TASK
    store.cache_source("github", TASK, {"title": "Mobile flow", "objective": contract["objective"], "labels": []}, "OPEN")
    canonical = canonical_contract_json(parse_contract(contract))
    import hashlib

    digest = hashlib.sha256(canonical.encode()).hexdigest()
    store.bind_task_contract(TASK, baseline, 1, digest, canonical)
    store.add_candidate(TASK, CANDIDATE, "claude", True)
    return project, store, contract_for_candidate(store, TASK, CANDIDATE, project)


def _review_failure(store: Store, message: str, remediation: str | None = None) -> None:
    item = {"severity": "error", "message": message}
    if remediation:
        item["remediation"] = remediation
    store.upsert_finding(finding_identity(CANDIDATE, message), TASK, CANDIDATE, "error", message)
    store.add_evidence(
        TASK,
        CANDIDATE,
        EvidenceKind.REVIEW,
        EvidenceStatus.FAILED,
        {"review_response": json.dumps({"decision": "FAIL_WITH_FINDINGS", "findings": [item]}), "findings": [item]},
    )


def test_legacy_missing_evidence_language_is_not_misrouted_as_code() -> None:
    assert classify_review_finding("The required npm run check has no recorded result and is unverified.") == VALIDATION
    assert classify_review_finding(
        "The task also requires npm run check, which runs validation and builds. "
        "The supplied evidence does not record that command passing."
    ) == VALIDATION
    assert classify_review_finding("No browser evidence at 360 pixels or desktop was recorded.") == EXTERNAL_EVIDENCE
    assert classify_review_finding("The timeout path lacks a test and the handler drops the error.") != VALIDATION


def test_changed_profile_gate_invalidates_only_validation_reuse_not_the_frozen_scope(tmp_path: Path) -> None:
    project, store, bound = _project(tmp_path)
    store.add_evidence(
        TASK,
        CANDIDATE,
        EvidenceKind.VALIDATION,
        EvidenceStatus.PASSED,
        {**bound.evidence_payload(), "gates": [{"name": OLD_GATE["name"], "status": "PASSED", "command": OLD_GATE["command"]}]},
    )

    augmented, policy = contract_with_current_profile_gates(store, project, TASK, bound)

    assert augmented.allowed_files == bound.contract.allowed_files == ("README.md",)
    assert [tuple(gate.command) for gate in augmented.required_tests] == [
        tuple(OLD_GATE["command"]),
        tuple(CURRENT_GATE["command"]),
    ]
    assert policy is not None and current_validation_status(store, project, TASK, CANDIDATE, bound) is None
    store.add_evidence(
        TASK,
        CANDIDATE,
        EvidenceKind.VALIDATION,
        EvidenceStatus.PASSED,
        {**bound.evidence_payload(), "current_profile_validation": policy},
    )
    assert current_validation_status(store, project, TASK, CANDIDATE, bound) is EvidenceStatus.PASSED


def test_current_profile_overlay_executes_both_frozen_and_current_gates(tmp_path: Path) -> None:
    project, store, bound = _project(tmp_path)
    (project / "README.md").write_text("candidate\n", encoding="utf-8")
    git = GitWorkspace(project)
    git.run("add", "README.md")
    git.run("commit", "-m", "candidate")
    candidate = git.head()
    augmented, policy = contract_with_current_profile_gates(store, project, TASK, bound)

    result = evaluate_contract(
        project,
        candidate,
        augmented,
        baseline_sha=bound.baseline_sha,
        run_gates=True,
    )

    assert policy is not None and result.passed
    assert [(gate.name, gate.status) for gate in result.gates] == [
        ("project-acceptance", "PASSED"),
        ("current-profile:project-acceptance", "PASSED"),
    ]


def test_existing_candidate_with_validation_finding_returns_to_validation_without_implementation(tmp_path: Path) -> None:
    project, store, _bound = _project(tmp_path)
    store.advance_task(TASK, Stage.IMPLEMENT)
    message = "The required project check has no recorded result and is unverified."
    _review_failure(store, message)  # legacy provider response without the new typed field
    store.add_task_remediation(TASK, str(Stage.REVIEW), CANDIDATE)
    coordinator = Coordinator(store, project)

    assert coordinator._reroute_evidence_remediation(TASK) is True
    assert store.get_task(TASK)["stage"] == Stage.VALIDATE
    assert store.conn.execute(
        "SELECT COUNT(*) FROM executions WHERE task_id=? AND kind=?", (TASK, ExecutionKind.IMPLEMENTATION)
    ).fetchone()[0] == 0
    assert review_remediations(store, TASK, CANDIDATE)[0].kind == VALIDATION


def test_existing_candidate_with_external_evidence_gap_blocks_once_without_implementation(tmp_path: Path) -> None:
    project, store, bound = _project(tmp_path, generated=False)
    store.advance_task(TASK, Stage.IMPLEMENT)
    store.add_evidence(
        TASK, CANDIDATE, EvidenceKind.VALIDATION, EvidenceStatus.PASSED, bound.evidence_payload()
    )
    message = "The supplied record lacks 360-pixel and desktop browser evidence and internal acceptance."
    _review_failure(store, message, EXTERNAL_EVIDENCE)
    store.add_task_remediation(TASK, str(Stage.REVIEW), CANDIDATE)
    coordinator = Coordinator(store, project)

    assert coordinator._reroute_evidence_remediation(TASK) is True
    task = store.get_task(TASK)
    assert (task["stage"], task["status"]) == (Stage.REVIEW, TaskStatus.BLOCKED)
    assert store.conn.execute(
        "SELECT COUNT(*) FROM executions WHERE task_id=? AND kind=?", (TASK, ExecutionKind.IMPLEMENTATION)
    ).fetchone()[0] == 0
    block = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type='task.block_classified' ORDER BY rowid DESC"
    ).fetchone()
    assert block is not None and json.loads(block["payload"])["failure_code"] == "external_evidence_required"


def test_existing_candidate_with_human_decision_blocks_without_implementation(tmp_path: Path) -> None:
    project, store, _bound = _project(tmp_path, generated=False)
    store.advance_task(TASK, Stage.IMPLEMENT)
    message = "The owner must choose which approved design frame controls this layout."
    _review_failure(store, message, HUMAN_DECISION)
    store.add_task_remediation(TASK, str(Stage.REVIEW), CANDIDATE)

    assert Coordinator(store, project)._reroute_evidence_remediation(TASK) is True
    task = store.get_task(TASK)
    assert (task["stage"], task["status"]) == (Stage.REVIEW, TaskStatus.BLOCKED)
    assert store.conn.execute(
        "SELECT COUNT(*) FROM executions WHERE task_id=? AND kind=?", (TASK, ExecutionKind.IMPLEMENTATION)
    ).fetchone()[0] == 0
    block = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type='task.block_classified' ORDER BY rowid DESC"
    ).fetchone()
    assert block is not None and json.loads(block["payload"])["failure_code"] == "human_decision_required"
