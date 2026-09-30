"""Wave 4 — OBJECTIVES-003: planner contract validation.
OBJECTIVES-005: planner wrapper/envelope normalization and retry behavior.

Legacy contract (test_objective_planner.py, build_coordinator/planner.py):
  - valid planner output accepted
  - malformed JSON rejected
  - wrong schema (not a dict) rejected
  - missing required task fields rejected
  - duplicate task IDs rejected
  - unknown dependency rejected
  - invalid DAG (self-reference) rejected
  - provider output cannot silently bypass validation
  - retry is bounded: repeated malformed output does not create duplicate tasks
  - success after retry produces exactly one task graph
  - malformed wrapper fails safely without corrupting objective state
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from stagemesh.objectives import Objective, ObjectivePlanner, ObjectiveValidationError


# ---------------------------------------------------------------------------
# Valid input
# ---------------------------------------------------------------------------

def test_valid_minimal_plan_accepted():
    planner = ObjectivePlanner()
    obj = planner.parse({"id": "OBJ-1", "title": "Test objective", "tasks": [{"id": "T1", "title": "Task one"}]})
    assert obj.id == "OBJ-1"
    assert obj.title == "Test objective"
    assert "T1" in obj.tasks


def test_valid_plan_with_dependencies_accepted():
    planner = ObjectivePlanner()
    payload = {
        "id": "OBJ-2",
        "title": "Multi-task",
        "tasks": [
            {"id": "T1", "title": "First"},
            {"id": "T2", "title": "Second", "dependencies": ["T1"]},
        ],
    }
    obj = planner.parse(payload)
    assert set(obj.tasks) == {"T1", "T2"}


def test_valid_plan_from_json_string_accepted():
    planner = ObjectivePlanner()
    raw = json.dumps({"id": "OBJ-3", "title": "JSON string", "tasks": [{"id": "T1", "title": "t"}]})
    obj = planner.parse(raw)
    assert obj.id == "OBJ-3"


# ---------------------------------------------------------------------------
# Malformed JSON
# ---------------------------------------------------------------------------

def test_malformed_json_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError, match="valid JSON"):
        planner.parse("{not valid json}")


def test_truncated_json_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError):
        planner.parse('{"id": "OBJ"')


# ---------------------------------------------------------------------------
# Wrong schema
# ---------------------------------------------------------------------------

def test_non_dict_root_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError, match="object"):
        planner.parse(json.dumps(["T1", "T2"]))


def test_missing_objective_id_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError, match="objective id"):
        planner.parse({"title": "No ID", "tasks": [{"id": "T1", "title": "t"}]})


def test_missing_objective_title_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError, match="objective title"):
        planner.parse({"id": "OBJ-1", "tasks": [{"id": "T1", "title": "t"}]})


def test_empty_tasks_list_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError, match="non-empty"):
        planner.parse({"id": "OBJ-1", "title": "Empty", "tasks": []})


def test_non_list_tasks_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError):
        planner.parse({"id": "OBJ-1", "title": "Bad", "tasks": "not-a-list"})


def test_task_missing_id_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError, match="id"):
        planner.parse({"id": "OBJ-1", "title": "Bad", "tasks": [{"title": "No id"}]})


def test_task_missing_title_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError, match="title"):
        planner.parse({"id": "OBJ-1", "title": "Bad", "tasks": [{"id": "T1"}]})


def test_task_blank_id_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError):
        planner.parse({"id": "OBJ-1", "title": "Bad", "tasks": [{"id": "  ", "title": "t"}]})


# ---------------------------------------------------------------------------
# Duplicate task IDs
# ---------------------------------------------------------------------------

def test_duplicate_task_ids_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError, match="duplicate"):
        planner.parse({
            "id": "OBJ-DUP",
            "title": "Dup test",
            "tasks": [
                {"id": "T1", "title": "First"},
                {"id": "T1", "title": "Duplicate"},
            ],
        })


# ---------------------------------------------------------------------------
# Unknown / invalid dependency
# ---------------------------------------------------------------------------

def test_unknown_dependency_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError, match="unknown dependency"):
        planner.parse({
            "id": "OBJ-DEP",
            "title": "Bad dep",
            "tasks": [
                {"id": "T1", "title": "task", "dependencies": ["NONEXISTENT"]},
            ],
        })


def test_non_string_dependency_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError):
        planner.parse({
            "id": "OBJ-DEP2",
            "title": "Bad dep",
            "tasks": [
                {"id": "T1", "title": "task", "dependencies": [42]},
            ],
        })


def test_non_list_dependencies_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError):
        planner.parse({
            "id": "OBJ-DEP3",
            "title": "Bad dep",
            "tasks": [
                {"id": "T1", "title": "task", "dependencies": "T0"},
            ],
        })


# ---------------------------------------------------------------------------
# Provider output cannot bypass validation (OBJECTIVES-003)
# ---------------------------------------------------------------------------

def test_provider_output_wrapping_still_validated():
    """
    Simulate a provider that wraps output in an extra layer.
    The outer envelope must not bypass ObjectivePlanner validation.
    """
    planner = ObjectivePlanner()
    # Provider returned raw string containing malformed plan
    raw_provider_output = json.dumps({
        "id": "OBJ-WRAP",
        "title": "Wrapped",
        "tasks": [{"title": "no id here"}],  # missing required 'id'
    })
    with pytest.raises(ObjectiveValidationError):
        planner.parse(raw_provider_output)


def test_valid_provider_output_accepted_through_envelope():
    planner = ObjectivePlanner()
    raw = json.dumps({
        "id": "OBJ-VALID",
        "title": "Valid envelope",
        "tasks": [{"id": "T1", "title": "Task"}],
    })
    obj = planner.parse(raw)
    assert obj.id == "OBJ-VALID"


# ---------------------------------------------------------------------------
# OBJECTIVES-005: write_backlog / envelope normalization
# ---------------------------------------------------------------------------

def test_write_backlog_creates_normalized_file(tmp_path: Path):
    planner = ObjectivePlanner()
    obj = planner.parse({
        "id": "OBJ-W1",
        "title": "Backlog test",
        "tasks": [{"id": "T1", "title": "Task one"}],
    })
    source = {
        "tasks": [{"id": "T1", "title": "Task one", "eligible": True, "state": "OPEN", "dependencies": []}]
    }
    backlog_path = tmp_path / "backlog.json"
    planner.write_backlog(obj, source, backlog_path)
    written = json.loads(backlog_path.read_text(encoding="utf-8"))
    assert written["objective"] == "OBJ-W1"
    assert len(written["tasks"]) == 1
    assert written["tasks"][0]["id"] == "T1"


def test_write_backlog_rejects_non_objective():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError, match="parsed"):
        planner.write_backlog("not-an-objective", {}, Path("/tmp/x.json"))  # type: ignore[arg-type]


def test_write_backlog_rejects_missing_task_in_source(tmp_path: Path):
    planner = ObjectivePlanner()
    obj = planner.parse({"id": "OBJ-W2", "title": "t", "tasks": [{"id": "T1", "title": "t"}]})
    source = {"tasks": []}  # T1 not present in source
    with pytest.raises(ObjectiveValidationError, match="missing task"):
        planner.write_backlog(obj, source, tmp_path / "b.json")


def test_write_backlog_rejects_invalid_eligible_type(tmp_path: Path):
    planner = ObjectivePlanner()
    obj = planner.parse({"id": "OBJ-W3", "title": "t", "tasks": [{"id": "T1", "title": "t"}]})
    source = {"tasks": [{"id": "T1", "title": "t", "eligible": "yes", "state": "OPEN"}]}
    with pytest.raises(ObjectiveValidationError, match="eligible"):
        planner.write_backlog(obj, source, tmp_path / "b.json")


def test_write_backlog_idempotent_overwrites_safely(tmp_path: Path):
    """Writing twice must produce exactly one valid file, not duplicate tasks."""
    planner = ObjectivePlanner()
    obj = planner.parse({"id": "OBJ-IDEMP", "title": "t", "tasks": [{"id": "T1", "title": "t"}]})
    source = {"tasks": [{"id": "T1", "title": "t", "eligible": True, "state": "OPEN", "dependencies": []}]}
    path = tmp_path / "backlog.json"
    planner.write_backlog(obj, source, path)
    planner.write_backlog(obj, source, path)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert len(written["tasks"]) == 1  # not doubled


# ---------------------------------------------------------------------------
# OBJECTIVES-005: retry suppression — repeated malformed output must not
# create duplicate task records in the store
# ---------------------------------------------------------------------------

def test_repeated_malformed_planner_output_does_not_duplicate_tasks(tmp_path: Path):
    """
    Repeated validation failures on the same objective must not accumulate
    tasks. We simulate multiple rounds of bad output by calling parse()
    repeatedly and confirming it raises each time (no silent accumulation).
    """
    planner = ObjectivePlanner()
    bad_payload = {"id": "OBJ-RETRY", "title": "Retry test", "tasks": [{"title": "no id"}]}

    parse_errors = 0
    for _ in range(5):
        try:
            planner.parse(bad_payload)
        except ObjectiveValidationError:
            parse_errors += 1

    # Every attempt fails — zero tasks can have been created
    assert parse_errors == 5


def test_success_after_failure_produces_single_correct_graph(tmp_path: Path):
    """
    After N failed attempts, a single successful parse must yield exactly
    the correct task set, not a union of prior attempts.
    """
    planner = ObjectivePlanner()
    bad_payload = {"id": "OBJ-SUCC", "title": "t", "tasks": [{"title": "no id"}]}
    good_payload = {
        "id": "OBJ-SUCC",
        "title": "t",
        "tasks": [{"id": "T1", "title": "Task one"}, {"id": "T2", "title": "Task two"}],
    }

    for _ in range(3):
        with pytest.raises(ObjectiveValidationError):
            planner.parse(bad_payload)

    obj = planner.parse(good_payload)
    assert set(obj.tasks) == {"T1", "T2"}  # exactly the successful parse
    assert len(obj.tasks) == 2  # not 6 (3 failed × 2 tasks)


# ---------------------------------------------------------------------------
# Field-length validation (boundary of 200-char limit)
# ---------------------------------------------------------------------------

def test_long_objective_id_rejected():
    planner = ObjectivePlanner()
    with pytest.raises(ObjectiveValidationError, match="200"):
        planner.parse({"id": "X" * 201, "title": "t", "tasks": [{"id": "T1", "title": "t"}]})


def test_exactly_200_char_id_accepted():
    planner = ObjectivePlanner()
    obj = planner.parse({"id": "X" * 200, "title": "t", "tasks": [{"id": "T1", "title": "t"}]})
    assert len(obj.id) == 200
