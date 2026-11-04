from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest
from test_run_ready import _project, _run
from test_spec_planning_gate import _contract_digest, _spec

from stagemesh.config import ConfigValidationError, load_config
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.planning_execution import (
    CHALLENGER_RECEIPT,
    PLANNER_RECEIPT,
    planning_execution_receipts,
)
from stagemesh.recovery import task_doctor
from stagemesh.spec_planning import (
    NEXT_APPROVE_SPEC,
    NEXT_REVISE_SPEC,
    freeze_planning_challenge,
    freeze_planning_spec,
    load_planning_artifacts,
    load_planning_challenge,
    planning_artifact_paths,
)

PROVIDER = r'''
import json, pathlib, sys
mode = sys.argv[1]
if mode == "mutate":
    pathlib.Path("README.md").write_text("provider-local mutation\n", encoding="utf-8")
if mode in {"planner", "mutate"}:
    print(json.dumps({
        "behavior": ["Keep the requested behavior bounded"],
        "non_goals": ["Do not widen the contract"],
        "affected_paths": ["README.md"],
        "acceptance_checks": ["inspect the exact candidate"],
        "decisions": [],
        "spec_refs": [],
    }))
elif mode == "challenger":
    print(json.dumps({
        "verdict": "accept",
        "findings": [{"kind": "missing-test", "summary": "Inspect README explicitly"}],
        "required_decisions": [],
    }))
elif mode == "revise":
    print(json.dumps({
        "verdict": "revise",
        "findings": [{"kind": "scope-risk", "summary": "Narrow the affected paths"}],
        "required_decisions": [],
    }))
else:
    print("not json")
'''


def _config(
    script: Path, *, planner_mode: str = "planner", challenger_mode: str = "challenger"
) -> dict:
    planner = shlex.join([sys.executable, str(script), planner_mode])
    challenger = shlex.join([sys.executable, str(script), challenger_mode])
    return {
        "providers": {"planner": planner, "challenger": challenger},
        "authorization": {"unattended_providers": ["planner", "challenger"]},
        "planning_gate": {
            "require_approved_spec": True,
            "require_plan_challenge": True,
            "planner_provider": "planner",
            "challenger_provider": "challenger",
        },
    }


def _script(tmp_path: Path) -> Path:
    path = tmp_path / "planning_provider.py"
    path.write_text(PROVIDER, encoding="utf-8")
    return path


def test_run_ready_executes_distinct_roles_then_stops_for_human_approval(tmp_path: Path):
    project = _project(tmp_path, ["T-1"], config=_config(_script(tmp_path)))
    before = (project / "README.md").read_text(encoding="utf-8")

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate"
    assert result["detail"]["next_action"] == NEXT_APPROVE_SPEC
    assert (project / "README.md").read_text(encoding="utf-8") == before
    spec, approval = load_planning_artifacts(project, "T-1")
    challenge = load_planning_challenge(project, "T-1")
    assert approval is None
    assert freeze_planning_spec(spec).digest == challenge["planning_spec_hash"]
    assert challenge["planner_provider"] == "planner"
    assert challenge["challenger_provider"] == "challenger"
    assert freeze_planning_challenge(challenge).digest

    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    receipts = planning_execution_receipts(store, "T-1")
    assert receipts["planner"]["provider_invoked"] is True
    assert receipts["challenger"]["provider_invoked"] is True
    assert receipts["planner"]["execution_verified"] is True
    assert receipts["challenger"]["execution_verified"] is True
    assert receipts["planner"]["command_digest"] != receipts["challenger"]["command_digest"]
    assert store.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type IN (?, ?)",
        (PLANNER_RECEIPT, CHALLENGER_RECEIPT),
    ).fetchone()[0] == 2
    report = task_doctor(store, project, "T-1")
    assert report["planning"]["planner_execution"]["provider"] == "planner"
    assert report["planning"]["challenger_execution"]["provider"] == "challenger"
    assert "Keep the requested behavior bounded" not in json.dumps(report)
    store.close()


def test_approved_executed_artifacts_are_reused_without_another_provider_call(tmp_path: Path):
    project = _project(tmp_path, ["T-1"], config=_config(_script(tmp_path)))
    _run(project)
    spec, _ = load_planning_artifacts(project, "T-1")
    challenge = load_planning_challenge(project, "T-1")
    _, approval_path = planning_artifact_paths(project, "T-1")
    approval_path.write_text(
        json.dumps(
            {
                "approved": True,
                "planning_spec_hash": freeze_planning_spec(spec).digest,
                "planning_challenge_hash": freeze_planning_challenge(challenge).digest,
            }
        ),
        encoding="utf-8",
    )
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    before = store.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type IN (?, ?)",
        (PLANNER_RECEIPT, CHALLENGER_RECEIPT),
    ).fetchone()[0]
    store.close()

    code, result = _run(project)

    assert code == 0 and result["stop_reason"] == "DONE", result
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    after = store.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type IN (?, ?)",
        (PLANNER_RECEIPT, CHALLENGER_RECEIPT),
    ).fetchone()[0]
    assert after == before == 2
    store.close()


def test_revise_verdict_is_preserved_and_blocks_implementation(tmp_path: Path):
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode="revise"),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate"
    assert result["detail"]["next_action"] == NEXT_REVISE_SPEC
    assert load_planning_challenge(project, "T-1")["verdict"] == "revise"


def test_malformed_challenger_output_fails_closed_after_trusted_planner(tmp_path: Path):
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode="malformed"),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_execution"
    assert result["detail"]["next_action"] == "challenger_invalid"
    assert load_planning_artifacts(project, "T-1")[0] is not None
    assert load_planning_challenge(project, "T-1") is None
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    statuses = [row["status"] for row in store.conn.execute("SELECT status FROM executions ORDER BY started_at")]
    assert statuses == ["SUCCEEDED", "FAILED"]
    store.close()


def test_distinct_names_with_the_same_command_are_not_independent(tmp_path: Path):
    script = _script(tmp_path)
    config = _config(script)
    config["providers"]["challenger"] = config["providers"]["planner"]
    project = _project(tmp_path, ["T-1"], config=config)

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_execution"
    assert result["detail"]["next_action"] == "planning_roles_not_independent"
    assert load_planning_artifacts(project, "T-1")[0] is not None
    assert load_planning_challenge(project, "T-1") is None


def test_explicit_provider_authorization_refusal_happens_before_launch(tmp_path: Path):
    config = _config(_script(tmp_path))
    config["authorization"]["unattended_providers"] = ["challenger"]
    project = _project(tmp_path, ["T-1"], config=config)

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_execution"
    assert result["detail"]["next_action"] == "provider_not_authorized"
    assert load_planning_artifacts(project, "T-1")[0] is None
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    store.close()


def test_planner_source_mutation_is_rejected_and_never_reaches_the_project(tmp_path: Path):
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), planner_mode="mutate"),
    )
    before = (project / "README.md").read_text(encoding="utf-8")

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_execution"
    assert result["detail"]["next_action"] == "planner_mutated_source"
    assert (project / "README.md").read_text(encoding="utf-8") == before
    assert load_planning_artifacts(project, "T-1")[0] is None
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    assert [row["status"] for row in store.conn.execute("SELECT status FROM executions")] == [
        "FAILED"
    ]
    store.close()


def test_configured_planner_does_not_treat_a_manual_provider_name_as_execution_proof(
    tmp_path: Path,
):
    project = _project(tmp_path, ["T-1"], config=_config(_script(tmp_path)))
    spec = _spec(
        contract_hash=_contract_digest(), baseline_sha=GitWorkspace(project).head()
    )
    spec_path, _ = planning_artifact_paths(project, "T-1")
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_execution"
    assert result["detail"]["next_action"] == "planner_execution_unverified"
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    store.close()


def test_provider_command_drift_invalidates_prior_execution_receipt(tmp_path: Path):
    script = _script(tmp_path)
    config = _config(script)
    project = _project(tmp_path, ["T-1"], config=config)
    _run(project)
    config["providers"]["planner"] = shlex.join(
        [sys.executable, str(script), "planner", "changed-command-identity"]
    )
    (project / ".stagemesh" / "config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_execution"
    assert result["detail"]["next_action"] == "planner_execution_unverified"
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 2
    store.close()


def test_config_rejects_untrusted_or_same_provider_role_combinations(tmp_path: Path):
    runtime = tmp_path / ".stagemesh"
    runtime.mkdir()
    config_path = runtime / "config.json"
    cases = [
        {
            "providers": {"one": "one"},
            "planning_gate": {
                "require_approved_spec": True,
                "require_plan_challenge": True,
                "challenger_provider": "one",
            },
        },
        {
            "providers": {"one": "one"},
            "planning_gate": {
                "require_approved_spec": True,
                "require_plan_challenge": True,
                "planner_provider": "one",
                "challenger_provider": "one",
            },
        },
        {
            "planning_gate": {
                "require_approved_spec": True,
                "planner_provider": "unknown",
            },
        },
        {
            "planning_gate": {
                "require_approved_spec": True,
                "planner_provider": True,
            },
        },
    ]
    for payload in cases:
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ConfigValidationError):
            load_config(tmp_path)
