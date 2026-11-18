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
    PlanningExecutionError,
    _freeze_planner,
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
if mode in {"planner", "mutate", "wrapped-planner", "aggregate-planner"}:
    payload = {
        "behavior": ["Keep the requested behavior bounded"],
        "non_goals": ["Do not widen the contract"],
        "affected_paths": ["README.md"],
        "acceptance_checks": ["inspect the exact candidate"],
        "decisions": [],
        "spec_refs": [],
    }
    if mode == "wrapped-planner":
        print("Here is the requested object:\n```json\n" + json.dumps(payload) + "\n```")
    elif mode == "aggregate-planner":
        payload["behavior"] = [f"behavior-{index:02d}-" + "b" * 120 for index in range(20)]
        payload["non_goals"] = [f"non-goal-{index:02d}-" + "n" * 120 for index in range(20)]
        payload["acceptance_checks"] = [f"check-{index:02d}-" + "c" * 120 for index in range(20)]
        print(json.dumps(payload))
    else:
        print(json.dumps(payload))
elif mode in {
    "challenger",
    "wrapped-challenger",
    "ambiguous-challenger",
    "aggregate-challenger",
    "array-prose-challenger",
    "escaped-challenger",
    "nested-duplicate-challenger",
    "truncated-array-challenger",
}:
    payload = {
        "verdict": "accept",
        "findings": [{"kind": "missing-test", "summary": "Inspect README explicitly"}],
        "required_decisions": [],
    }
    if mode == "wrapped-challenger":
        print("Independent challenge complete.\n```json\n" + json.dumps(payload) + "\n```\nEnd.")
    elif mode == "ambiguous-challenger":
        print(json.dumps(payload) + "\n" + json.dumps({**payload, "verdict": "revise"}))
    elif mode == "aggregate-challenger":
        payload["findings"] = [
            {"kind": "missing-test", "summary": f"finding-{index:02d}-" + "x" * 390}
            for index in range(20)
        ]
        print(json.dumps(payload))
    elif mode == "escaped-challenger":
        payload["findings"] = [
            {"kind": "missing-test", "summary": "😀" * 500},
            {"kind": "scope-risk", "summary": "🚀" * 145},
        ]
        print(json.dumps(payload))
    elif mode == "array-prose-challenger":
        print("Result: [" + json.dumps(payload) + "] done")
    elif mode == "nested-duplicate-challenger":
        print(
            '{"verdict":"revise","findings":['
            '{"kind":"missing-test","kind":"scope-risk","summary":"x"}'
            '],"required_decisions":[]}'
        )
    elif mode == "truncated-array-challenger":
        print("[" + json.dumps(payload))
    else:
        print(json.dumps(payload))
elif mode == "revise":
    print(json.dumps({
        "verdict": "revise",
        "findings": [{"kind": "scope-risk", "summary": "Narrow the affected paths"}],
        "required_decisions": [],
    }))
elif mode == "long-challenger":
    print(json.dumps({
        "verdict": "accept",
        "findings": [{"kind": "missing-test", "summary": "x" * 669}],
        "required_decisions": [],
    }))
elif mode == "duplicate-key-challenger":
    print('{"verdict":"revise","verdict":"accept","findings":[],"required_decisions":[]}')
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


def test_long_provider_finding_is_visibly_bounded_instead_of_refusing_planning(
    tmp_path: Path,
):
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode="long-challenger"),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate"
    assert result["detail"]["next_action"] == NEXT_APPROVE_SPEC
    challenge = load_planning_challenge(project, "T-1")
    summary = challenge["findings"][0]["summary"]
    assert len(summary) == 500
    assert summary.endswith("... [truncated by StageMesh]")
    assert freeze_planning_challenge(challenge).digest
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    assert [row["status"] for row in store.conn.execute("SELECT status FROM executions ORDER BY started_at")] == [
        "SUCCEEDED",
        "SUCCEEDED",
    ]
    store.close()


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


def test_wrapped_provider_objects_complete_both_planning_roles(tmp_path: Path):
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(
            _script(tmp_path),
            planner_mode="wrapped-planner",
            challenger_mode="wrapped-challenger",
        ),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate"
    assert result["detail"]["next_action"] == NEXT_APPROVE_SPEC
    assert load_planning_artifacts(project, "T-1")[0] is not None
    assert load_planning_challenge(project, "T-1")["verdict"] == "accept"


def test_multiple_provider_objects_remain_ambiguous_and_fail_closed(tmp_path: Path):
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode="ambiguous-challenger"),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_execution"
    assert result["detail"]["next_action"] == "challenger_invalid"
    assert load_planning_challenge(project, "T-1") is None


@pytest.mark.parametrize("mode", ["aggregate-challenger", "escaped-challenger"])
def test_locally_valid_challenge_is_fitted_to_the_canonical_budget(
    tmp_path: Path, mode: str
) -> None:
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode=mode),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate"
    assert result["detail"]["next_action"] == NEXT_APPROVE_SPEC
    challenge = load_planning_challenge(project, "T-1")
    frozen = freeze_planning_challenge(challenge)
    assert len(frozen.canonical_json) <= 8_000
    assert len(challenge["findings"]) == (20 if mode == "aggregate-challenger" else 2)
    assert all(item["summary"] for item in challenge["findings"])
    assert any(item["summary"].endswith("... [truncated by StageMesh]") for item in challenge["findings"])


def test_locally_valid_planner_output_is_fitted_to_the_canonical_budget(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), planner_mode="aggregate-planner"),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate"
    assert result["detail"]["next_action"] == NEXT_APPROVE_SPEC
    spec, _ = load_planning_artifacts(project, "T-1")
    frozen = freeze_planning_spec(spec)
    assert len(frozen.canonical_json) <= 8_000
    assert len(spec["behavior"]) == len(spec["non_goals"]) == len(spec["acceptance_checks"]) == 20
    assert any(item.endswith("... [truncated by StageMesh]") for item in spec["behavior"])


def test_duplicate_provider_json_keys_fail_closed(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode="duplicate-key-challenger"),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_execution"
    assert result["detail"]["next_action"] == "challenger_invalid"
    assert load_planning_challenge(project, "T-1") is None


@pytest.mark.parametrize(
    "mode",
    [
        "array-prose-challenger",
        "nested-duplicate-challenger",
        "truncated-array-challenger",
    ],
)
def test_ambiguous_json_wrappers_and_nested_duplicate_keys_fail_closed(
    tmp_path: Path, mode: str
) -> None:
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode=mode),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_execution"
    assert result["detail"]["next_action"] == "challenger_invalid"
    assert load_planning_challenge(project, "T-1") is None


def test_validation_text_cannot_spoof_an_over_budget_error_code() -> None:
    payload = {
        "behavior": ["bounded"],
        "non_goals": [],
        "affected_paths": ["README.md"],
        "acceptance_checks": ["inspect"],
        "decisions": [{"question": "cannot fit", "resolution": ""}],
        "spec_refs": [],
    }

    with pytest.raises(PlanningExecutionError) as exc:
        _freeze_planner(payload, "a" * 64, "b" * 40)

    assert exc.value.code == "planner_invalid"


def test_new_planning_artifact_is_published_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stagemesh import planning_execution

    target = tmp_path / "planning.json"

    def fail_link(*_args, **_kwargs):
        raise OSError("simulated publication interruption")

    monkeypatch.setattr(planning_execution.os, "link", fail_link, raising=False)

    with pytest.raises(PlanningExecutionError) as exc:
        planning_execution._write_new(target, '{"valid":true}')

    assert exc.value.code == "planning_artifact_publish_failed"
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_missing_artifact_after_receipt_is_recoverable_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stagemesh import planning_execution

    project = _project(tmp_path, ["T-1"], config=_config(_script(tmp_path)))
    write_new = planning_execution._write_new
    attempts = 0

    def interrupt_once(path, text):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PlanningExecutionError(
                "planning_artifact_publish_failed", "simulated publication interruption"
            )
        return write_new(path, text)

    monkeypatch.setattr(planning_execution, "_write_new", interrupt_once)

    first_code, first = _run(project)
    second_code, second = _run(project)

    assert first_code != 0
    assert first["detail"]["next_action"] == "planning_artifact_publish_failed"
    assert second_code != 0 and second["stop_reason"] == "REFUSED:planning_gate"
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        planner_receipts = store.conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type=?", (PLANNER_RECEIPT,)
        ).fetchone()[0]
        challenger_receipts = store.conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type=?", (CHALLENGER_RECEIPT,)
        ).fetchone()[0]
    finally:
        store.close()
    assert (planner_receipts, challenger_receipts) == (2, 1)


def test_execution_receipt_is_durable_before_each_artifact_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stagemesh import planning_execution

    project = _project(tmp_path, ["T-1"], config=_config(_script(tmp_path)))
    events: list[str] = []
    record_receipt = planning_execution._record_receipt
    write_new = planning_execution._write_new

    def observe_receipt(*args, **kwargs):
        events.append(f"receipt:{args[1]}")
        return record_receipt(*args, **kwargs)

    def observe_write(path, text):
        events.append(f"write:{path.name}")
        return write_new(path, text)

    monkeypatch.setattr(planning_execution, "_record_receipt", observe_receipt)
    monkeypatch.setattr(planning_execution, "_write_new", observe_write)

    _run(project)

    assert events == [
        f"receipt:{PLANNER_RECEIPT}",
        "write:T-1.spec.json",
        f"receipt:{CHALLENGER_RECEIPT}",
        "write:T-1.challenge.json",
    ]


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
