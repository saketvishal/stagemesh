from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest
from test_run_ready import _project, _run
from test_spec_planning_gate import _contract_digest, _spec

import stagemesh.cli as cli_module
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.planning_execution import (
    CHALLENGER_RECEIPT,
    PLANNER_RECEIPT,
    PLANNING_PROTOCOL_RETRY,
    PlanningExecutionError,
    _freeze_planner,
    planning_execution_receipts,
)
from stagemesh.recovery import task_doctor
from stagemesh.spec_planning import (
    CONTRACT_AWARE_CHALLENGE_CUTOVER_EPOCH,
    MAX_AUTOMATIC_PLANNING_REVISIONS,
    NEXT_APPROVE_SPEC,
    NEXT_REVISE_SPEC,
    PLANNING_CHALLENGE_RETIRED_EVENT,
    PLANNING_CHALLENGE_REVIEW_PROTOCOL_VERSION,
    PLANNING_REVISION_ABORTED_EVENT,
    PLANNING_REVISION_COMPLETED_EVENT,
    PLANNING_REVISION_INTENT_EVENT,
    PlanningChallengeRefreshRefusal,
    freeze_planning_challenge,
    freeze_planning_spec,
    load_planning_artifacts,
    load_planning_challenge,
    planning_artifact_paths,
    publish_automatic_planning_revision,
    retire_planning_challenge,
)

PROVIDER = r'''
import json, pathlib, sys
mode = sys.argv[1]
prompt = sys.stdin.read()
if mode == "mutate":
    pathlib.Path("README.md").write_text("provider-local mutation\n", encoding="utf-8")
if mode in {
    "planner",
    "mutate",
    "wrapped-planner",
    "aggregate-planner",
    "invalid-revision-spec-refs",
    "always-invalid-spec-refs",
}:
    payload = {
        "behavior": ["Keep the requested behavior bounded"],
        "non_goals": ["Do not widen the contract"],
        "affected_paths": ["README.md"],
        "acceptance_checks": ["inspect the exact candidate"],
        "decisions": [],
        "spec_refs": [],
    }
    if "Independent review findings:" in prompt:
        payload["behavior"].append("Incorporate independent review findings")
        if "Incorporate independent review findings" in prompt:
            payload["behavior"].append("Resolve follow-up review findings")
    correction = "StageMesh rejected the previous response" in prompt
    if mode == "invalid-revision-spec-refs" and "Independent review findings:" in prompt:
        payload["spec_refs"] = ["README.md"] if correction else ["C:/outside/spec.md"]
    elif mode == "always-invalid-spec-refs":
        payload["spec_refs"] = ["Issue #137 and PR #139", "README.md for context"]
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
    "invalid-kind-once",
    "always-invalid-kind",
}:
    payload = {
        "verdict": "accept",
        "findings": [{"kind": "missing-test", "summary": "Inspect README explicitly"}],
        "required_decisions": [],
    }
    correction = "StageMesh rejected the previous response" in prompt
    if mode == "invalid-kind-once" and not correction:
        payload["findings"] = [
            {"kind": "accessibility-risk", "summary": "Verify narrow screen behavior"},
            {"kind": "performance-risk", "summary": "Verify fixture loading cost"},
        ]
        print(json.dumps(payload))
    elif mode == "always-invalid-kind":
        payload["findings"] = [
            {"kind": "accessibility-risk", "summary": "Verify narrow screen behavior"},
        ]
        print(json.dumps(payload))
    elif mode == "wrapped-challenger":
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
elif mode == "revise-until-updated":
    covered = "Incorporate independent review findings" in prompt
    print(json.dumps({
        "verdict": "accept" if covered else "revise",
        "findings": [] if covered else [{"kind": "scope-risk", "summary": "Narrow the affected paths"}],
        "required_decisions": [],
    }))
elif mode == "revise-twice-until-updated":
    converged = "Resolve follow-up review findings" in prompt
    print(json.dumps({
        "verdict": "accept" if converged else "revise",
        "findings": [] if converged else [{"kind": "scope-risk", "summary": "Resolve the remaining blocker"}],
        "required_decisions": [],
    }))
elif mode == "human":
    print(json.dumps({
        "verdict": "needs-human-decision",
        "findings": [{"kind": "scope-risk", "summary": "Authorization was not visible"}],
        "required_decisions": ["Confirm whether this task is authorized"],
    }))
elif mode == "contract-aware":
    covered = "REQUIRED_CONTRACT_BEHAVIOR" in prompt
    print(json.dumps({
        "verdict": "accept" if covered else "revise",
        "findings": [] if covered else [{"kind": "objective-drift", "summary": "Contract behavior was omitted"}],
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

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate", result
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
    assert receipts["challenger"]["review_protocol_version"] == PLANNING_CHALLENGE_REVIEW_PROTOCOL_VERSION
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


def test_new_task_is_bound_before_accountable_approval(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    config = _config(_script(tmp_path), challenger_mode="revise-until-updated")
    config["planning_gate"]["approval_authority"] = {
        "version": 1,
        "authority_id": "project-owner:test",
        "approver_id": "owner:test",
    }
    project = _project(tmp_path, ["T-1"], config=config)

    code, result = _run(project)

    assert code != 0 and result["detail"]["next_action"] == "obtain_planning_approval"
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        assert store.task_baseline("T-1") == GitWorkspace(project).head()
        assert store.task_contract("T-1") is not None
    finally:
        store.close()

    approved = cli_module.main(
        ["--project", str(project), "approve-planning", "--task", "T-1", "--json"]
    )
    approval = json.loads(capsys.readouterr().out)
    assert approved == 0 and approval["approver_id"] == "owner:test"


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


def test_revise_verdict_gets_two_bounded_revisions_then_blocks_if_findings_remain(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode="revise"),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate", result
    assert result["detail"]["next_action"] == NEXT_REVISE_SPEC
    assert load_planning_challenge(project, "T-1")["verdict"] == "revise"
    spec, _approval = load_planning_artifacts(project, "T-1")
    assert "Incorporate independent review findings" in spec["behavior"]
    assert len(result["detail"]["planning_findings"]) == 1
    assert ".stagemesh/planning/T-1.challenge.json" in result["message"]
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 6
        assert len(list(store.task_audit_events(PLANNING_REVISION_INTENT_EVENT, "T-1"))) == 2
        completed = list(
            store.task_audit_events(PLANNING_REVISION_COMPLETED_EVENT, "T-1")
        )
        assert len(completed) == MAX_AUTOMATIC_PLANNING_REVISIONS
        assert [json.loads(row["payload"])["revision_round"] for row in completed] == [
            1,
            2,
        ]
        before = store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
    finally:
        store.close()

    second_code, second = _run(project)

    assert second_code != 0 and second["detail"]["next_action"] == NEXT_REVISE_SPEC
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == before
    finally:
        store.close()


def test_revise_without_human_decisions_is_replanned_and_rechallenged_once(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode="revise-until-updated"),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate", result
    assert result["detail"]["next_action"] == NEXT_APPROVE_SPEC
    spec, _approval = load_planning_artifacts(project, "T-1")
    challenge = load_planning_challenge(project, "T-1")
    assert "Incorporate independent review findings" in spec["behavior"]
    assert challenge["verdict"] == "accept"
    assert challenge["planning_spec_hash"] == freeze_planning_spec(spec).digest
    history = project / ".stagemesh" / "planning" / "history"
    assert len(list(history.glob("T-1.spec.*.json"))) == 1
    assert len(list(history.glob("T-1.challenge.*.json"))) == 1
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 4
        assert len(list(store.task_audit_events(PLANNING_REVISION_COMPLETED_EVENT, "T-1"))) == 1
    finally:
        store.close()


def test_follow_up_challenge_converges_after_second_bounded_revision(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(
            _script(tmp_path), challenger_mode="revise-twice-until-updated"
        ),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate", result
    assert result["detail"]["next_action"] == NEXT_APPROVE_SPEC
    spec, _approval = load_planning_artifacts(project, "T-1")
    challenge = load_planning_challenge(project, "T-1")
    assert "Incorporate independent review findings" in spec["behavior"]
    assert "Resolve follow-up review findings" in spec["behavior"]
    assert challenge["verdict"] == "accept"
    history = project / ".stagemesh" / "planning" / "history"
    assert len(list(history.glob("T-1.spec.*.json"))) == 2
    assert len(list(history.glob("T-1.challenge.*.json"))) == 2
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 6
        completed = list(
            store.task_audit_events(PLANNING_REVISION_COMPLETED_EVENT, "T-1")
        )
        assert len(completed) == MAX_AUTOMATIC_PLANNING_REVISIONS
    finally:
        store.close()


def test_existing_single_revision_state_resumes_after_runtime_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stagemesh import planning_execution

    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode="revise"),
    )
    monkeypatch.setattr(planning_execution, "MAX_AUTOMATIC_PLANNING_REVISIONS", 1)

    first_code, first = _run(project)

    assert first_code != 0 and first["detail"]["next_action"] == NEXT_REVISE_SPEC
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        assert len(
            list(store.task_audit_events(PLANNING_REVISION_COMPLETED_EVENT, "T-1"))
        ) == 1
        assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 4
    finally:
        store.close()

    monkeypatch.setattr(
        planning_execution,
        "MAX_AUTOMATIC_PLANNING_REVISIONS",
        MAX_AUTOMATIC_PLANNING_REVISIONS,
    )
    second_code, second = _run(project)

    assert second_code != 0 and second["detail"]["next_action"] == NEXT_REVISE_SPEC
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        completed = list(
            store.task_audit_events(PLANNING_REVISION_COMPLETED_EVENT, "T-1")
        )
        assert len(completed) == MAX_AUTOMATIC_PLANNING_REVISIONS
        assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 6
    finally:
        store.close()


def test_interrupted_planning_revision_restores_both_authoritative_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stagemesh import spec_planning

    project = _project(tmp_path, ["T-1"])
    current_spec = _spec(
        contract_hash=_contract_digest(), baseline_sha=GitWorkspace(project).head()
    )
    current_frozen = freeze_planning_spec(current_spec)
    challenge = {
        "version": 1,
        "planning_spec_hash": current_frozen.digest,
        "planner_provider": "planner",
        "challenger_provider": "challenger",
        "verdict": "revise",
        "findings": [{"kind": "missing-test", "summary": "Add the missing test"}],
        "required_decisions": [],
    }
    spec_path, _approval_path = planning_artifact_paths(project, "T-1")
    challenge_path = spec_path.with_name("T-1.challenge.json")
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(current_spec), encoding="utf-8")
    challenge_path.write_text(json.dumps(challenge), encoding="utf-8")
    revised_spec = {**current_spec, "behavior": ["Revised bounded behavior"]}
    original_replace = spec_planning.os.replace
    replacements = 0

    def interrupt_second_replace(source, destination):
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("simulated artifact transition interruption")
        return original_replace(source, destination)

    monkeypatch.setattr(spec_planning.os, "replace", interrupt_second_replace)
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        store.migrate()
        assert store.upsert_task("task T-1", source_id="T-1") == "T-1"
        with pytest.raises(OSError, match="simulated artifact transition interruption"):
            publish_automatic_planning_revision(
                store,
                project,
                "T-1",
                current_spec_payload=current_spec,
                challenge_payload=challenge,
                revised_spec_payload=revised_spec,
            )
        assert freeze_planning_spec(json.loads(spec_path.read_text(encoding="utf-8"))).digest == current_frozen.digest
        assert freeze_planning_challenge(json.loads(challenge_path.read_text(encoding="utf-8"))).digest == freeze_planning_challenge(challenge).digest
        assert len(list(store.task_audit_events(PLANNING_REVISION_ABORTED_EVENT, "T-1"))) == 1
        assert list(store.task_audit_events(PLANNING_REVISION_COMPLETED_EVENT, "T-1")) == []
    finally:
        store.close()


def test_accountable_gate_surfaces_challenge_decisions_before_missing_approval(tmp_path: Path):
    config = _config(_script(tmp_path), challenger_mode="human")
    config["planning_gate"]["approval_authority"] = {
        "version": 1,
        "authority_id": "project-owner:example",
        "approver_id": "owner:example",
    }
    project = _project(tmp_path, ["T-1"], config=config)

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate"
    assert result["detail"]["next_action"] == "resolve_planning_challenge_decisions"
    assert result["detail"]["required_decisions"] == ["Confirm whether this task is authorized"]
    assert "Confirm whether this task is authorized" in result["message"]
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 2
        assert list(store.task_audit_events(PLANNING_REVISION_INTENT_EVENT, "T-1")) == []
    finally:
        store.close()


def test_current_protocol_human_decision_cannot_be_rerolled(tmp_path: Path):
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode="human"),
    )
    code, result = _run(project)
    assert code != 0 and result["detail"]["next_action"] == "resolve_planning_challenge_decisions"
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        with pytest.raises(PlanningChallengeRefreshRefusal, match="already used the contract-aware review protocol"):
            retire_planning_challenge(store, project, "T-1", reason="try the same review again")
    finally:
        store.close()


def test_challenger_receives_authoritative_task_contract(tmp_path: Path):
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode="contract-aware"),
    )
    contract_path = project / ".stagemesh" / "contracts" / "T-1.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["objective"] = "REQUIRED_CONTRACT_BEHAVIOR"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    code, result = _run(project)

    assert code != 0 and result["detail"]["next_action"] == NEXT_APPROVE_SPEC
    assert load_planning_challenge(project, "T-1")["verdict"] == "accept"
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        assert planning_execution_receipts(store, "T-1")["challenger"]["execution_verified"] is True
    finally:
        store.close()


def test_explicit_refresh_preserves_old_decision_then_reruns_current_independent_challenger(tmp_path: Path):
    script = _script(tmp_path)
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(script, challenger_mode="human"),
    )
    contract_path = project / ".stagemesh" / "contracts" / "T-1.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["objective"] = "REQUIRED_CONTRACT_BEHAVIOR"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    code, result = _run(project)
    assert code != 0 and result["detail"]["next_action"] == "resolve_planning_challenge_decisions"
    old_challenge = load_planning_challenge(project, "T-1")
    old_digest = freeze_planning_challenge(old_challenge).digest

    config_path = project / ".stagemesh" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["providers"]["challenger"] = shlex.join([sys.executable, str(script), "contract-aware"])
    config_path.write_text(json.dumps(config), encoding="utf-8")
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        rows = store.conn.execute(
            "SELECT rowid, payload FROM audit_events WHERE event_type=?",
            (CHALLENGER_RECEIPT,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("artifact_digest") == old_digest:
                payload.pop("review_protocol_version", None)  # artifact produced before the protocol receipt existed
                store.conn.execute(
                    "UPDATE audit_events SET payload=?, created_at=? WHERE rowid=?",
                    (
                        json.dumps(payload, sort_keys=True),
                        CONTRACT_AWARE_CHALLENGE_CUTOVER_EPOCH - 1,
                        row["rowid"],
                    ),
                )
        store.conn.commit()
        retired = retire_planning_challenge(
            store,
            project,
            "T-1",
            reason="the current challenger now receives the authoritative task contract",
        )
        assert retired["retired_challenge_hash"] == old_digest
        assert json.loads((project / retired["archive_path"]).read_text(encoding="utf-8"))["verdict"] == "needs-human-decision"
        assert len(list(store.task_audit_events(PLANNING_CHALLENGE_RETIRED_EVENT, "T-1"))) == 1
    finally:
        store.close()

    code, result = _run(project)

    assert code != 0 and result["detail"]["next_action"] == NEXT_APPROVE_SPEC
    assert load_planning_challenge(project, "T-1")["verdict"] == "accept"
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        challenger_events = list(store.task_audit_events(CHALLENGER_RECEIPT, "T-1"))
        assert len(challenger_events) == 2
        assert planning_execution_receipts(store, "T-1")["challenger"]["execution_verified"] is True
    finally:
        store.close()


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
    assert statuses == ["SUCCEEDED", "FAILED", "FAILED"]
    assert store.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type=?",
        (PLANNING_PROTOCOL_RETRY,),
    ).fetchone()[0] == 1
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


def test_invalid_revision_spec_refs_receive_one_bounded_protocol_correction(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(
            _script(tmp_path),
            planner_mode="invalid-revision-spec-refs",
            challenger_mode="revise-until-updated",
        ),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate"
    assert result["detail"]["next_action"] == NEXT_APPROVE_SPEC
    spec, _ = load_planning_artifacts(project, "T-1")
    assert spec["spec_refs"] == ["README.md"]
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        statuses = [
            row["status"]
            for row in store.conn.execute("SELECT status FROM executions ORDER BY started_at")
        ]
        assert statuses == ["SUCCEEDED", "SUCCEEDED", "FAILED", "SUCCEEDED", "SUCCEEDED"]
        assert store.conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type=?",
            (PLANNING_PROTOCOL_RETRY,),
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_persistently_invalid_spec_refs_stop_after_one_correction(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), planner_mode="always-invalid-spec-refs"),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_execution"
    assert result["detail"]["next_action"] == "planner_invalid"
    assert load_planning_artifacts(project, "T-1")[0] is None
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        statuses = [
            row["status"]
            for row in store.conn.execute("SELECT status FROM executions ORDER BY started_at")
        ]
        assert statuses == ["FAILED", "FAILED"]
        assert store.conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type=?",
            (PLANNING_PROTOCOL_RETRY,),
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_invalid_challenger_kinds_receive_one_bounded_protocol_correction(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode="invalid-kind-once"),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate"
    assert result["detail"]["next_action"] == NEXT_APPROVE_SPEC
    challenge = load_planning_challenge(project, "T-1")
    assert challenge["findings"] == [
        {"kind": "missing-test", "summary": "Inspect README explicitly"}
    ]
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        statuses = [
            row["status"]
            for row in store.conn.execute("SELECT status FROM executions ORDER BY started_at")
        ]
        assert statuses == ["SUCCEEDED", "FAILED", "SUCCEEDED"]
        event = store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type=?",
            (PLANNING_PROTOCOL_RETRY,),
        ).fetchone()
        assert json.loads(event["payload"])["protocol_role"] == "challenger"
    finally:
        store.close()


def test_persistently_invalid_challenger_kind_stops_after_one_correction(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        ["T-1"],
        config=_config(_script(tmp_path), challenger_mode="always-invalid-kind"),
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_execution"
    assert result["detail"]["next_action"] == "challenger_invalid"
    assert load_planning_artifacts(project, "T-1")[0] is not None
    assert load_planning_challenge(project, "T-1") is None
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        statuses = [
            row["status"]
            for row in store.conn.execute("SELECT status FROM executions ORDER BY started_at")
        ]
        assert statuses == ["SUCCEEDED", "FAILED", "FAILED"]
        assert store.conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type=?",
            (PLANNING_PROTOCOL_RETRY,),
        ).fetchone()[0] == 1
    finally:
        store.close()


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


def test_accountably_approved_plan_survives_later_provider_command_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _script(tmp_path)
    config = _config(script)
    config["planning_gate"]["approval_authority"] = {
        "version": 1,
        "authority_id": "project-owner:test",
        "approver_id": "owner:test",
    }
    project = _project(tmp_path, ["T-1"], config=config)
    code, result = _run(project)
    assert code != 0 and result["detail"]["next_action"] == "obtain_planning_approval", result
    assert cli_module.main(
        ["--project", str(project), "approve-planning", "--task", "T-1", "--json"]
    ) == 0
    capsys.readouterr()

    config["providers"]["planner"] = shlex.join(
        [sys.executable, str(script), "planner", "changed-command-identity"]
    )
    (project / ".stagemesh" / "config.json").write_text(
        json.dumps(config), encoding="utf-8"
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


def test_stale_accountable_approval_does_not_bypass_provider_command_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _script(tmp_path)
    config = _config(script)
    config["planning_gate"]["approval_authority"] = {
        "version": 1,
        "authority_id": "project-owner:test",
        "approver_id": "owner:test",
    }
    project = _project(tmp_path, ["T-1"], config=config)
    _run(project)
    assert cli_module.main(
        ["--project", str(project), "approve-planning", "--task", "T-1", "--json"]
    ) == 0
    capsys.readouterr()

    spec_path, _approval_path = planning_artifact_paths(project, "T-1")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["behavior"].append("unapproved artifact mutation")
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    config["providers"]["planner"] = shlex.join(
        [sys.executable, str(script), "planner", "changed-command-identity"]
    )
    (project / ".stagemesh" / "config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )

    code, result = _run(project)

    assert code != 0 and result["stop_reason"] == "REFUSED:planning_execution"
    assert result["detail"]["next_action"] == "planner_execution_unverified"


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
