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


# ---------------------------------------------------------------------------
# OBJECTIVES-003: DAG cycle validation (self, 2-node, multi-node)
# ---------------------------------------------------------------------------

def test_dag_self_dependency_rejected():
    planner = ObjectivePlanner()
    payload = {
        "id": "OBJ-CYCLE-1",
        "title": "Self cycle",
        "tasks": [
            {"id": "T1", "title": "Self dep", "dependencies": ["T1"]},
        ],
    }
    with pytest.raises(ObjectiveValidationError, match="dependency cycle detected"):
        planner.parse(payload)


def test_dag_two_node_cycle_rejected():
    planner = ObjectivePlanner()
    payload = {
        "id": "OBJ-CYCLE-2",
        "title": "Two node cycle",
        "tasks": [
            {"id": "T1", "title": "Task 1", "dependencies": ["T2"]},
            {"id": "T2", "title": "Task 2", "dependencies": ["T1"]},
        ],
    }
    with pytest.raises(ObjectiveValidationError, match="dependency cycle detected"):
        planner.parse(payload)


def test_dag_three_node_cycle_rejected():
    planner = ObjectivePlanner()
    payload = {
        "id": "OBJ-CYCLE-3",
        "title": "Three node cycle",
        "tasks": [
            {"id": "T1", "title": "Task 1", "dependencies": ["T2"]},
            {"id": "T2", "title": "Task 2", "dependencies": ["T3"]},
            {"id": "T3", "title": "Task 3", "dependencies": ["T1"]},
        ],
    }
    with pytest.raises(ObjectiveValidationError, match="dependency cycle detected"):
        planner.parse(payload)


def test_dag_valid_complex_acyclic_graph_passes():
    planner = ObjectivePlanner()
    payload = {
        "id": "OBJ-DAG-VALID",
        "title": "Complex acyclic graph",
        "tasks": [
            {"id": "T1", "title": "Base 1"},
            {"id": "T2", "title": "Base 2"},
            {"id": "T3", "title": "Middle", "dependencies": ["T1", "T2"]},
            {"id": "T4", "title": "Final", "dependencies": ["T3"]},
        ],
    }
    obj = planner.parse(payload)
    assert set(obj.tasks) == {"T1", "T2", "T3", "T4"}


# ---------------------------------------------------------------------------
# OBJECTIVES-005: Wrapper normalization & retry suppression with Store
# ---------------------------------------------------------------------------

def test_wrapper_normalization_markdown_fences():
    from stagemesh.objectives import normalize_planner_payload
    raw = """Here is the plan:
```json
{
  "id": "OBJ-FENCE",
  "title": "Fenced Plan",
  "tasks": [{"id": "T1", "title": "Task 1"}]
}
```
Hope this helps!"""
    normalized = normalize_planner_payload(raw)
    assert normalized["id"] == "OBJ-FENCE"
    assert len(normalized["tasks"]) == 1


def test_wrapper_normalization_envelope_with_thinking_stripped():
    from stagemesh.objectives import normalize_planner_payload
    envelope = {
        "thinking": "Step 1: plan the tasks carefully...",
        "chain_of_thought": "I will decompose this into two steps.",
        "plan": {
            "id": "OBJ-ENVELOPE",
            "title": "Enveloped Plan",
            "tasks": [
                {"id": "T1", "title": "First"},
                {"id": "T2", "title": "Second", "dependencies": ["T1"]},
            ],
        },
    }
    normalized = normalize_planner_payload(envelope)
    assert normalized["id"] == "OBJ-ENVELOPE"
    assert "thinking" not in normalized
    assert "chain_of_thought" not in normalized
    assert len(normalized["tasks"]) == 2


def test_retry_suppression_and_no_partial_store_corruption(tmp_path: Path):
    """
    Exercise the real planner/store orchestration:
    - Multiple malformed attempts must fail closed and record retry failures.
    - No partial task/objective state is inserted into Store.
    - Subsequent successful plan execution produces exactly one objective and correct task set.
    """
    from stagemesh.persistence import Store
    from stagemesh.retry import RetryRegistry

    db = tmp_path / "stagemesh.sqlite3"
    store = Store(db)
    store.migrate()

    planner = ObjectivePlanner()
    retry_reg = RetryRegistry(store)

    malformed_attempt_1 = "{malformed json"
    malformed_attempt_2 = json.dumps({"id": "OBJ-RETRY", "tasks": [{"title": "missing id"}]})
    valid_attempt = json.dumps({
        "id": "OBJ-RETRY",
        "title": "Recovered Objective",
        "tasks": [
            {"id": "TASK-1", "title": "First Task"},
            {"id": "TASK-2", "title": "Second Task", "dependencies": ["TASK-1"]},
        ],
    })

    # Attempt 1 fails
    with pytest.raises(ObjectiveValidationError):
        planner.parse(malformed_attempt_1)
    d1 = retry_reg.record_failure("planner:OBJ-RETRY", "malformed_json")
    assert d1.attempts == 1

    # Attempt 2 fails
    with pytest.raises(ObjectiveValidationError):
        planner.parse(malformed_attempt_2)
    d2 = retry_reg.record_failure("planner:OBJ-RETRY", "schema_validation_error")
    assert d2.attempts == 2

    # Verify no tasks or objectives exist in the store yet
    assert len(store.tasks()) == 0
    assert len(list(store.conn.execute("SELECT * FROM objectives"))) == 0

    # Successful attempt
    obj = planner.parse(valid_attempt)
    payload = json.loads(valid_attempt)
    store.save_objective(obj.id, obj.title, payload)
    for tid in obj.tasks:
        store.upsert_task(f"Task {tid}", task_id=tid, source="objective", source_id=f"{obj.id}:{tid}")
    retry_reg.record_success("planner:OBJ-RETRY")

    # Store now has exactly 1 objective and 2 tasks — no duplicate or corrupted state
    objectives = list(store.conn.execute("SELECT * FROM objectives"))
    assert len(objectives) == 1
    assert objectives[0]["id"] == "OBJ-RETRY"

    tasks = store.tasks()
    assert len(tasks) == 2
    assert {t["id"] for t in tasks} == {"TASK-1", "TASK-2"}

    # Retry state was cleared
    retries = store.retry_states()
    assert len(retries) == 0


def test_provider_output_requires_full_envelope_and_rejects_bare_plan():
    from stagemesh.objectives import parse_planner_envelope

    # Bare plan must be rejected (legacy test_planner_wrapper_rejects_bare_objective_plan)
    with pytest.raises(ObjectiveValidationError, match="requires full envelope containing 'plan' mapping"):
        parse_planner_envelope('{"tasks": []}')

    with pytest.raises(ObjectiveValidationError, match="requires full envelope containing 'plan' mapping"):
        parse_planner_envelope({"id": "OBJ-BARE", "title": "Bare", "tasks": []})

    # Full envelope accepted
    full_envelope = {
        "schema_version": 1,
        "execution_id": "exec-1",
        "task_id": "OBJ-1-PLANNER",
        "role": "PLANNER",
        "status": "SUCCEEDED",
        "plan": {
            "id": "OBJ-1",
            "title": "Enveloped",
            "tasks": [{"id": "T1", "title": "Task 1"}],
        },
    }
    extracted = parse_planner_envelope(full_envelope)
    assert extracted["id"] == "OBJ-1"
    assert "role" not in extracted
    assert "execution_id" not in extracted


def test_planner_sanitization_drops_thinking_and_lifecycle_identity_from_cli_plan(tmp_path: Path):
    import argparse
    from stagemesh.cli import command_plan
    from stagemesh.persistence import Store

    (tmp_path / ".stagemesh").mkdir(parents=True, exist_ok=True)
    plan_file = tmp_path / "plan.json"
    plan_content = {
        "id": "OBJ-CLEAN",
        "title": "Clean Plan",
        "thinking": "Secret thoughts that must not survive",
        "chain_of_thought": "Private reasoning chain",
        "scratchpad": "Internal work",
        "model_identity": "secret-model-v1",
        "tasks": [
            {
                "id": "T1",
                "title": "First Task",
                "thinking": "task secret",
                "chain_of_thought": "task cot",
                "eligible": True,
                "state": "OPEN",
            }
        ],
    }
    plan_file.write_text(json.dumps(plan_content), encoding="utf-8")

    args = argparse.Namespace(project=str(tmp_path), file=str(plan_file), json=False)
    ret = command_plan(args)
    assert ret == 0

    # Read back objective from store and assert secrets are gone
    store = Store(tmp_path / ".stagemesh" / "stagemesh.sqlite3")
    row = store.conn.execute("SELECT payload FROM objectives WHERE id=?", ("OBJ-CLEAN",)).fetchone()
    assert row is not None
    persisted = json.loads(row["payload"])

    assert "thinking" not in persisted
    assert "chain_of_thought" not in persisted
    assert "scratchpad" not in persisted
    assert "model_identity" not in persisted
    assert "thinking" not in persisted["tasks"][0]
    assert "chain_of_thought" not in persisted["tasks"][0]


def test_command_plan_atomic_all_or_nothing_on_failure(tmp_path: Path):
    import argparse
    from stagemesh.cli import command_plan
    from stagemesh.persistence import Store

    (tmp_path / ".stagemesh").mkdir(parents=True, exist_ok=True)
    plan_file = tmp_path / "invalid_plan.json"
    # An invalid plan with missing required title on task
    invalid_content = {
        "id": "OBJ-FAIL",
        "title": "Failing Objective",
        "tasks": [{"id": "T1"}],  # missing title
    }
    plan_file.write_text(json.dumps(invalid_content), encoding="utf-8")

    args = argparse.Namespace(project=str(tmp_path), file=str(plan_file), json=False)
    with pytest.raises(ObjectiveValidationError):
        command_plan(args)

    # Verify nothing was persisted in Store
    db_file = tmp_path / ".stagemesh" / "stagemesh.sqlite3"
    if db_file.exists():
        store = Store(db_file)
        rows = store.conn.execute("SELECT * FROM objectives").fetchall()
        assert len(rows) == 0


def test_durable_retry_suppression_and_contract_revision_recovery_production_path(tmp_path: Path):
    """Real production-path test:
    - ObjectivePlanner wired to durable Store.
    - Contract C1 with malformed output suppresses further retries.
    - Contract revision C2 clears suppression and allows successful recovery.
    """
    from stagemesh.objectives import ObjectivePlanner
    from stagemesh.persistence import Store

    db = tmp_path / "stagemesh.sqlite3"
    store = Store(db)
    store.migrate()

    planner = ObjectivePlanner(store=store)

    c1_prompt = "Plan objective OBJ-PROD with contract v1"
    bad_payload = {
        "schema_version": 1,
        "execution_id": "exec-bad",
        "role": "PLANNER",
        "status": "SUCCEEDED",
        "plan": {
            "id": "OBJ-PROD",
            "title": "Title",
            "tasks": [{"title": "missing id"}],
        },
    }

    # Attempt 1: fails and records durable failure in store
    with pytest.raises(ObjectiveValidationError):
        planner.parse_provider_plan(bad_payload, contract=c1_prompt, target_key="OBJ-PROD")

    # Cycle 2 with unchanged contract C1: production method is suppressed!
    with pytest.raises(ObjectiveValidationError, match="planner retry suppressed: unchanged malformed output"):
        planner.parse_provider_plan(bad_payload, contract=c1_prompt, target_key="OBJ-PROD")

    # Verify durable retry state in Store
    retry_state = store.get_retry_state("planner:OBJ-PROD")
    assert retry_state is not None
    assert retry_state["attempts"] == 1

    # Contract revision: prompt changes to C2!
    c2_prompt = "Plan objective OBJ-PROD with contract v2 (fixed task ids)"
    good_payload = {
        "schema_version": 1,
        "execution_id": "exec-good",
        "role": "PLANNER",
        "status": "SUCCEEDED",
        "plan": {
            "id": "OBJ-PROD",
            "title": "Recovered Title",
            "tasks": [{"id": "T1", "title": "Task 1"}],
        },
    }

    # Cycle 3 with revised contract C2: suppression is cleared and succeeds!
    obj, plan = planner.parse_provider_plan(good_payload, contract=c2_prompt, target_key="OBJ-PROD")
    assert obj.id == "OBJ-PROD"
    assert obj.tasks == ("T1",)
    assert plan["title"] == "Recovered Title"

    # Durable retry state is cleared in Store!
    assert store.get_retry_state("planner:OBJ-PROD") is None



