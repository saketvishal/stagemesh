"""Tests for enriched remediation diagnostics for failed validation gates and findings."""
from __future__ import annotations

import json
from pathlib import Path

from test_parallel import Rig

from stagemesh.diagnosis import diagnose
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.providers import _remediation_prompt
from stagemesh.remediation import remediation_context

TASK = "TASK-REMEDIATION-1"


def test_remediation_context_and_prompt_enrichment(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    sha = "c" * 40
    # Setup candidate and contract binding
    rig.store.add_candidate(TASK, sha, "tester", True)
    digest = "d" * 64
    rig.store.bind_contract(TASK, sha, "b" * 40, 1, digest, "{}")
    rig.store.add_task_remediation(TASK, Stage.VALIDATE.value, sha)

    # Insert a finding into store
    rig.store.upsert_finding("finding-1", TASK, sha, "error", "focused-tests failed")

    # Record failed validation evidence with detailed gate payload (as produced by validation runner)
    failed_payload = {
        "verdict": "FAILED",
        "findings": [
            {
                "id": "gate:focused-tests",
                "severity": "error",
                "message": "focused-tests failed",
                "stdout": "pytest session started...\nFAILED tests/test_core.py::test_fail\n",
                "stderr": "Command timed out after 240 seconds",
                "failure_kind": "timeout",
            }
        ],
        "gates": [
            {
                "name": "focused-tests",
                "command": ["pytest", "-q", "tests/test_core.py"],
                "returncode": -15,
                "duration_seconds": 240.2,
                "status": "FAILED",
                "stderr": "Command timed out after 240 seconds",
                "stdout": "running test suite...\nfailure at line 42",
            }
        ],
    }
    rig.store.add_evidence(TASK, sha, EvidenceKind.VALIDATION, EvidenceStatus.FAILED, failed_payload)

    # Test remediation_context enrichment
    ctx = remediation_context(rig.store, TASK, rig.project)
    assert ctx is not None
    assert ctx["candidate_sha"] == sha
    assert len(ctx["findings"]) == 1
    finding = ctx["findings"][0]
    assert finding["message"] == "focused-tests failed"
    assert finding["stderr"] == "Command timed out after 240 seconds"
    assert "pytest session started" in finding["stdout"]
    assert finding["failure_kind"] == "timeout"

    assert "failed_gates" in ctx
    assert len(ctx["failed_gates"]) == 1
    gate = ctx["failed_gates"][0]
    assert gate["name"] == "focused-tests"
    assert gate["returncode"] == -15
    assert gate["duration_seconds"] == 240.2
    assert gate["stderr"] == "Command timed out after 240 seconds"

    # Test remediation prompt formatting
    prompt = _remediation_prompt(ctx)
    assert "focused-tests failed" in prompt
    assert "stderr: Command timed out after 240 seconds" in prompt
    assert "Failing validation gates (command and failure output):" in prompt
    assert "Gate focused-tests (exit code -15 after 240.2s):" in prompt
    assert "command: pytest -q tests/test_core.py" in prompt


def test_progress_only_timeout_is_an_operational_gate_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    sha = "a" * 40
    rig.store.add_candidate(TASK, sha, "tester", True)
    rig.store.add_evidence(
        TASK,
        sha,
        EvidenceKind.VALIDATION,
        EvidenceStatus.FAILED,
        {
            "findings": [
                {
                    "code": "gate_failed",
                    "message": "stagemesh-lifecycle-smoke failed",
                    "failure_kind": "timeout",
                    "returncode": None,
                    "stdout": "tests/test_error_handling.py ...... [ 45%]\n",
                    "stderr": "timed out after 360s\n",
                }
            ]
        },
    )
    diagnosis = diagnose(rig.store, TASK, rig.project)
    assert diagnosis is not None
    assert diagnosis.category == "validation_gate"
    assert diagnosis.environment_unavailable is False
    assert diagnosis.inconclusive_validation is True
    assert diagnosis.stops_remediation is True
    assert "another implementation" in diagnosis.recommendation
    assert "environment fault" in diagnosis.recommendation


def test_timeout_that_captured_an_assertion_stays_a_code_defect(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    sha = "b" * 40
    rig.store.add_candidate(TASK, sha, "tester", True)
    rig.store.add_evidence(
        TASK,
        sha,
        EvidenceKind.VALIDATION,
        EvidenceStatus.FAILED,
        {
            "findings": [
                {
                    "code": "gate_failed",
                    "message": "focused-tests failed",
                    "failure_kind": "timeout",
                    "returncode": None,
                    "stdout": "FAILED tests/test_core.py::test_fail\nAssertionError: expected\n",
                    "stderr": "timed out after 240s\n",
                }
            ]
        },
    )
    diagnosis = diagnose(rig.store, TASK, rig.project)
    assert diagnosis is not None
    assert diagnosis.category == "implementation_defect"
    assert diagnosis.environment_unavailable is False
    assert diagnosis.inconclusive_validation is False
