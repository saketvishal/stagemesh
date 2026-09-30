from __future__ import annotations

import json
from pathlib import Path

import pytest

from stagemesh.execution import (
    ExecutionResult,
    ExecutionStatus,
    StructuredResultValidationError,
    parse_structured_result,
)


def test_parse_structured_result_valid_payload():
    payload = {
        "status": "SUCCEEDED",
        "candidate_sha": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        "durable_handoff": True,
        "failure_reason": None,
        "metadata": {"worker": "test-agent"},
    }
    result = parse_structured_result(payload, expected_task_id="TASK-100")
    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.candidate_sha == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    assert result.durable_handoff is True
    assert result.capacity_failure is False


def test_parse_structured_result_malformed_fails_closed():
    with pytest.raises(StructuredResultValidationError, match="status"):
        parse_structured_result({"candidate_sha": "abc"}, expected_task_id="TASK-100")


def test_parse_structured_result_invalid_json():
    with pytest.raises(StructuredResultValidationError, match="invalid JSON"):
        parse_structured_result("not a json string", expected_task_id="TASK-100")


def test_parse_structured_result_mismatched_candidate_sha():
    payload = {
        "status": "SUCCEEDED",
        "candidate_sha": "short",
        "durable_handoff": True,
    }
    with pytest.raises(StructuredResultValidationError, match="candidate_sha"):
        parse_structured_result(payload, expected_task_id="TASK-100")
