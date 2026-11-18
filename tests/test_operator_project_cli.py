from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from stagemesh import api_server, cli, spec_planning, ui_launcher
from stagemesh.persistence import Store
from stagemesh.registry import GlobalRegistry, ProjectRegistration
from stagemesh.spec_planning import (
    CONTRACT_AWARE_CHALLENGE_CUTOVER_EPOCH,
    PLANNING_CHALLENGE_RETIRE_ABORTED_EVENT,
    PLANNING_CHALLENGE_RETIRE_INTENT_EVENT,
    PLANNING_CHALLENGE_RETIRED_EVENT,
    PLANNING_CHALLENGE_REVIEW_PROTOCOL_VERSION,
    freeze_planning_challenge,
    freeze_planning_spec,
    reconcile_planning_challenge_retirement,
)
from stagemesh.stage_dispatch import connected_dispatch_database, stage_dispatch_lock


class FakeServer:
    def __init__(self, port: int):
        self.server_address = ("127.0.0.1", port)
        self.served = False
        self.closed = False
        self.stopped = False

    def serve_forever(self) -> None:
        self.served = True

    def shutdown(self) -> None:
        self.stopped = True

    def server_close(self) -> None:
        self.closed = True


class FakeTickets:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.count = 0

    def issue(self) -> str:
        self.count += 1
        return f"{self.prefix}-{self.count}"


def _project(path) -> None:
    path.mkdir()
    store = Store(cli.db_path(path))
    store.migrate()
    store.close()


def test_ui_registry_launches_separate_servers_tokens_and_projections(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _project(alpha)
    _project(beta)
    registry_path = tmp_path / "registry.json"
    GlobalRegistry(registry_path).save(
        [
            ProjectRegistration("Alpha", alpha, cli.db_path(alpha)),
            ProjectRegistration("Beta", beta, cli.db_path(beta)),
        ]
    )
    created: list[tuple[FakeServer, SimpleNamespace, object]] = []

    def fake_server(
        db,
        token=None,
        port=0,
        static_dir=None,
        api_class=None,
        control=None,
        projects=None,
        doctor=None,
        provider_status=None,
    ):
        index = len(created) + 1
        server = FakeServer(44000 + index)
        api = SimpleNamespace(
            token=f"token-{index}",
            launch_tickets=FakeTickets(f"ticket-{index}"),
            projects=None,
        )
        created.append((server, api, control))
        assert doctor is not None
        assert provider_status is not None
        return server, api

    opened: list[str] = []
    monkeypatch.setattr(api_server, "make_server", fake_server)
    monkeypatch.setattr(
        ui_launcher,
        "launch_ui",
        lambda url: opened.append(url) or ui_launcher.UiLaunch("browser-app", "browser"),
    )

    assert cli.main(
        [
            "--project",
            str(alpha),
            "ui",
            "--registry",
            str(registry_path),
            "--enable-control",
        ]
    ) == 0

    assert len(created) == 2
    assert created[0][0].server_address != created[1][0].server_address
    assert created[0][1].token != created[1][1].token
    assert all(control is not None for _server, _api, control in created)
    assert opened == ["http://127.0.0.1:44001/#token=token-1"]
    for server, api, _control in created:
        projection = api.projects.serialize()
        assert projection["current_project_id"]
        assert len(projection["projects"]) == 2
        assert all("token-" not in str(row["launch_url"]) for row in projection["projects"])
        assert server.closed


def test_ui_registry_refuses_to_infer_unregistered_current_project(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _project(alpha)
    _project(beta)
    registry_path = tmp_path / "registry.json"
    GlobalRegistry(registry_path).save(
        [ProjectRegistration("Beta", beta, cli.db_path(beta))]
    )
    monkeypatch.setattr(
        api_server,
        "make_server",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("server started")),
    )

    assert cli.main(
        ["--project", str(alpha), "ui", "--registry", str(registry_path)]
    ) == 2
    assert "no default was inferred" in capsys.readouterr().err


# --- approve-planning ---------------------------------------------------------------------------------------------
_APPROVAL_CONTRACT = "c" * 64
_APPROVAL_BASELINE = "b" * 40
_APPROVAL_AUTHORITY = {"version": 1, "authority_id": "project-owner:example", "approver_id": "owner:example"}


def _approval_project(path, *, authority: bool = True) -> str:
    """A synthetic project with a bound task and a valid frozen spec; returns the task id."""
    path.mkdir()
    store = Store(cli.db_path(path))
    store.migrate()
    task_id = store.upsert_task("synthetic")
    store.set_task_baseline(task_id, _APPROVAL_BASELINE)
    store.bind_task_contract(task_id, _APPROVAL_BASELINE, 1, _APPROVAL_CONTRACT, "{}")
    store.close()
    gate = {"require_approved_spec": True}
    if authority:
        gate["approval_authority"] = _APPROVAL_AUTHORITY
    (path / ".stagemesh" / "planning").mkdir(parents=True)
    (path / ".stagemesh" / "config.json").write_text(json.dumps({"planning_gate": gate}), encoding="utf-8")
    spec = {
        "behavior": ["do the thing"],
        "affected_paths": ["src/x.py"],
        "acceptance_checks": ["pytest tests/test_x.py"],
        "contract_hash": _APPROVAL_CONTRACT,
        "baseline_sha": _APPROVAL_BASELINE,
    }
    (path / ".stagemesh" / "planning" / f"{task_id}.spec.json").write_text(json.dumps(spec), encoding="utf-8")
    return task_id


def _approve(path, task_id: str, capsys, *extra: str) -> tuple[int, dict]:
    code = cli.main(["--project", str(path), "approve-planning", "--task", task_id, "--json", *extra])
    return code, json.loads(capsys.readouterr().out)


def _refreshable_challenge(
    path,
    task_id: str,
    *,
    verdict: str = "needs-human-decision",
    review_protocol: int | None = None,
    legacy_authorization: bool = True,
    legacy_receipt: bool = True,
) -> bytes:
    planning = path / ".stagemesh" / "planning"
    spec = json.loads((planning / f"{task_id}.spec.json").read_text(encoding="utf-8"))
    challenge = {
        "version": 1,
        "planning_spec_hash": freeze_planning_spec(spec).digest,
        "planner_provider": "planner",
        "challenger_provider": "challenger",
        "verdict": verdict,
        "findings": (
            []
            if verdict == "accept"
            else [{"kind": "human-decision", "summary": "The recorded context needs another independent review"}]
        ),
        "required_decisions": (
            ["Confirm whether this task is authorized for implementation"]
            if verdict == "needs-human-decision" and legacy_authorization
            else (["Choose the product retention period"] if verdict == "needs-human-decision" else [])
        ),
    }
    payload = json.dumps(challenge, indent=2).encode()
    (planning / f"{task_id}.challenge.json").write_bytes(payload)
    frozen = freeze_planning_challenge(challenge)
    store = Store(cli.db_path(path))
    execution_id = store.start_execution(
        task_id=task_id,
        claim_id=None,
        kind="REVIEW",
        actor="challenger",
    )
    store.finish_execution(execution_id, "SUCCEEDED", actor="challenger")
    receipt = {
        "task_id": task_id,
        "artifact_digest": frozen.digest,
        "provider": "challenger",
        "execution_id": execution_id,
        "provider_invoked": True,
    }
    if review_protocol is not None:
        receipt["review_protocol_version"] = review_protocol
    receipt_id = store.add_audit_event("planning.challenge_produced", receipt)
    if legacy_receipt:
        store.conn.execute(
            "UPDATE audit_events SET created_at=? WHERE id=?",
            (CONTRACT_AWARE_CHALLENGE_CUTOVER_EPOCH - 1, receipt_id),
        )
        store.conn.commit()
    store.close()
    return payload


def _refresh_challenge(path, task_id: str, capsys, reason: str = "the governing task now records the decision") -> tuple[int, dict]:
    code = cli.main(
        [
            "--project",
            str(path),
            "refresh-planning-challenge",
            "--task",
            task_id,
            "--reason",
            reason,
            "--json",
        ]
    )
    return code, json.loads(capsys.readouterr().out)


def test_approve_planning_records_receipt_artifact_and_returns_bound_identities(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)

    code, report = _approve(project, task_id, capsys)

    assert code == 0
    assert report["task_id"] == task_id
    assert report["approver_id"] == "owner:example"  # from protected config, not an argument
    assert report["authority_id"] == "project-owner:example"
    assert report["baseline_sha"] == _APPROVAL_BASELINE and report["contract_hash"] == _APPROVAL_CONTRACT
    assert report["planning_challenge_hash"] is None
    for key in ("planning_spec_hash", "authority_hash", "receipt_id"):
        assert report[key]
    artifact = json.loads((project / report["approval_path"]).read_text(encoding="utf-8"))
    assert artifact["receipt_id"] == report["receipt_id"] and artifact["version"] == 2
    store = Store(cli.db_path(project))
    try:
        assert [r["receipt_id"] for r in store.planning_approval_receipts(task_id)] == [report["receipt_id"]]
    finally:
        store.close()

    code, repeat = _approve(project, task_id, capsys)  # identical inputs: same receipt, nothing new recorded
    assert code == 0 and repeat["receipt_id"] == report["receipt_id"] and repeat["receipt_recorded"] is False


def test_approve_planning_prints_a_human_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)

    assert cli.main(["--project", str(project), "approve-planning", "--task", task_id]) == 0

    out = capsys.readouterr().out
    assert "owner:example" in out and _APPROVAL_BASELINE in out and _APPROVAL_CONTRACT in out


def test_approve_planning_refuses_provider_context_without_writing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STAGEMESH_PROVIDER_PROCESS", "1")
    project = tmp_path / "project"
    task_id = _approval_project(project)

    code, refusal = _approve(project, task_id, capsys)

    assert code == 2 and refusal["code"] == "operator_decision"
    assert not (project / ".stagemesh" / "planning" / f"{task_id}.approval.json").exists()


def test_approve_planning_refuses_when_authority_is_not_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project, authority=False)

    code, refusal = _approve(project, task_id, capsys)

    assert code == 2 and refusal["code"] == "approval_authority_missing"
    assert refusal["next_action"] == "repair_approval_authority"
    assert not (project / ".stagemesh" / "planning" / f"{task_id}.approval.json").exists()


def test_approve_planning_refuses_invalid_authority_config(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)
    gate = {"require_approved_spec": True, "approval_authority": {**_APPROVAL_AUTHORITY, "extra": 1}}
    (project / ".stagemesh" / "config.json").write_text(json.dumps({"planning_gate": gate}), encoding="utf-8")

    code, refusal = _approve(project, task_id, capsys)

    assert code == 2 and refusal["code"] == "invalid_config"


def test_approve_planning_refuses_active_execution_and_preserves_it(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)
    store = Store(cli.db_path(project))
    execution = store.start_execution(task_id=task_id, claim_id=None, kind="IMPLEMENTATION")
    store.close()

    code, refusal = _approve(project, task_id, capsys)

    assert code == 2 and refusal["code"] == "execution_active"
    store = Store(cli.db_path(project))
    try:
        assert store.planning_approval_receipts(task_id) == []
        assert store.active_execution_kinds(task_id) == {"IMPLEMENTATION"}
        assert execution
    finally:
        store.close()


def test_approve_planning_accepts_no_approver_override():
    parser = cli.build_parser()
    assert parser.parse_args(["approve-planning", "--task", "t-1", "--json"]).command == "approve-planning"
    for flag in ("--approver", "--approver-id", "--actor", "--config"):
        with pytest.raises(SystemExit):
            parser.parse_args(["approve-planning", "--task", "t-1", flag, "someone:else"])


# --- refresh-planning-challenge -----------------------------------------------------------------------------------


def test_refresh_planning_challenge_preserves_exact_artifact_and_records_audit(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)
    original = _refreshable_challenge(project, task_id)

    code, report = _refresh_challenge(project, task_id, capsys)

    assert code == 0 and report["retired_verdict"] == "needs-human-decision"
    assert not (project / ".stagemesh" / "planning" / f"{task_id}.challenge.json").exists()
    assert (project / report["archive_path"]).read_bytes() == original
    store = Store(cli.db_path(project))
    try:
        event = list(store.task_audit_events(PLANNING_CHALLENGE_RETIRED_EVENT, task_id))
        assert len(event) == 1
        payload = json.loads(event[0]["payload"])
        assert payload["artifact_digest"] == report["retired_challenge_hash"]
        assert payload["archive_path"] == report["archive_path"]
        assert payload["reason"] == "the governing task now records the decision"
    finally:
        store.close()
    code, refusal = _refresh_challenge(project, task_id, capsys, reason="try again")
    assert code == 2 and refusal["code"] == "challenge_missing"
    assert len(list((project / ".stagemesh" / "planning" / "history").glob("*.json"))) == 1


@pytest.mark.parametrize("protected", ["accepted", "approved", "receipt", "active", "claim"])
def test_refresh_planning_challenge_refuses_protected_state(tmp_path, monkeypatch, capsys, protected):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)
    _refreshable_challenge(project, task_id, verdict="accept" if protected == "accepted" else "needs-human-decision")
    if protected == "approved":
        (project / ".stagemesh" / "planning" / f"{task_id}.approval.json").write_text("{}", encoding="utf-8")
    if protected == "receipt":
        challenge = json.loads(
            (project / ".stagemesh" / "planning" / f"{task_id}.challenge.json").read_text(encoding="utf-8")
        )
        store = Store(cli.db_path(project))
        store.record_planning_approval(
            {
                "task_id": task_id,
                "receipt_id": "approval-existing",
                "planning_challenge_hash": freeze_planning_challenge(challenge).digest,
            }
        )
        store.close()
    if protected == "active":
        store = Store(cli.db_path(project))
        store.start_execution(task_id=task_id, claim_id=None, kind="REVIEW")
        store.close()
    if protected == "claim":
        store = Store(cli.db_path(project))
        assert store.acquire_claim(task_id, "test-worker") is not None
        store.close()

    code, refusal = _refresh_challenge(project, task_id, capsys)

    expected = {
        "accepted": "challenge_accepted",
        "approved": "approval_exists",
        "receipt": "approval_exists",
        "active": "execution_active",
        "claim": "claim_active",
    }
    assert code == 2 and refusal["code"] == expected[protected]
    assert (project / ".stagemesh" / "planning" / f"{task_id}.challenge.json").exists()
    history = project / ".stagemesh" / "planning" / "history"
    assert not history.exists() or not list(history.glob("*.json"))


def test_refresh_planning_challenge_refuses_provider_process(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    task_id = _approval_project(project)
    original = _refreshable_challenge(project, task_id)
    monkeypatch.setenv("STAGEMESH_PROVIDER_PROCESS", "1")

    code, refusal = _refresh_challenge(project, task_id, capsys)

    assert code == 2 and refusal["code"] == "operator_decision"
    assert (project / ".stagemesh" / "planning" / f"{task_id}.challenge.json").read_bytes() == original


def test_refresh_planning_challenge_cannot_reroll_current_protocol(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)
    original = _refreshable_challenge(
        project,
        task_id,
        review_protocol=PLANNING_CHALLENGE_REVIEW_PROTOCOL_VERSION,
    )

    code, refusal = _refresh_challenge(project, task_id, capsys)

    assert code == 2 and refusal["code"] == "challenge_context_unchanged"
    assert (project / ".stagemesh" / "planning" / f"{task_id}.challenge.json").read_bytes() == original


def test_refresh_planning_challenge_refuses_unlabelled_post_cutover_receipt(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)
    original = _refreshable_challenge(
        project,
        task_id,
        legacy_authorization=False,
        legacy_receipt=False,
    )

    code, refusal = _refresh_challenge(project, task_id, capsys)

    assert code == 2 and refusal["code"] == "challenge_context_unchanged"
    assert (project / ".stagemesh" / "planning" / f"{task_id}.challenge.json").read_bytes() == original


@pytest.mark.parametrize("defect", ["missing-receipt", "invalid-protocol", "actor-mismatch"])
def test_refresh_planning_challenge_requires_verified_challenger_receipt(
    tmp_path, monkeypatch, capsys, defect
):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)
    original = _refreshable_challenge(project, task_id)
    store = Store(cli.db_path(project))
    if defect == "missing-receipt":
        store.conn.execute("DELETE FROM audit_events WHERE event_type='planning.challenge_produced'")
    elif defect == "invalid-protocol":
        row = store.conn.execute(
            "SELECT rowid, payload FROM audit_events WHERE event_type='planning.challenge_produced'"
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["review_protocol_version"] = True
        store.conn.execute("UPDATE audit_events SET payload=? WHERE rowid=?", (json.dumps(payload), row["rowid"]))
    else:
        store.conn.execute("UPDATE executions SET actor='different-challenger' WHERE actor='challenger'")
    store.conn.commit()
    store.close()

    code, refusal = _refresh_challenge(project, task_id, capsys)

    assert code == 2 and refusal["code"] == "challenger_execution_unverified"
    assert (project / ".stagemesh" / "planning" / f"{task_id}.challenge.json").read_bytes() == original


def test_refresh_planning_challenge_allows_current_protocol_after_planning_inputs_change(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)
    _refreshable_challenge(
        project,
        task_id,
        review_protocol=PLANNING_CHALLENGE_REVIEW_PROTOCOL_VERSION,
        legacy_authorization=False,
    )
    spec_path = project / ".stagemesh" / "planning" / f"{task_id}.spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["decisions"] = [{"question": "Retention period", "resolution": "30 days"}]
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    code, report = _refresh_challenge(project, task_id, capsys, reason="the planning spec now records the decision")

    assert code == 0 and report["retired_verdict"] == "needs-human-decision"


def test_refresh_planning_challenge_restores_current_artifact_if_audit_write_fails(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)
    original = _refreshable_challenge(project, task_id)

    add_audit_event = Store.add_audit_event

    def fail_completion(self, event_type, payload=None):  # type: ignore[no-untyped-def]
        if event_type == PLANNING_CHALLENGE_RETIRED_EVENT:
            raise RuntimeError("audit unavailable")
        return add_audit_event(self, event_type, payload)

    monkeypatch.setattr(Store, "add_audit_event", fail_completion)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        cli.main(
            [
                "--project",
                str(project),
                "refresh-planning-challenge",
                "--task",
                task_id,
                "--reason",
                "context changed",
                "--json",
            ]
        )

    assert (project / ".stagemesh" / "planning" / f"{task_id}.challenge.json").read_bytes() == original
    history = project / ".stagemesh" / "planning" / "history"
    assert not list(history.glob("*.json"))
    store = Store(cli.db_path(project))
    try:
        assert len(list(store.task_audit_events(PLANNING_CHALLENGE_RETIRE_INTENT_EVENT, task_id))) == 1
        aborted = list(store.task_audit_events(PLANNING_CHALLENGE_RETIRE_ABORTED_EVENT, task_id))
        assert len(aborted) == 1 and json.loads(aborted[0]["payload"])["restored"] is True
        assert not list(store.task_audit_events(PLANNING_CHALLENGE_RETIRED_EVENT, task_id))
    finally:
        store.close()


def test_refresh_planning_challenge_refuses_concurrent_stage_dispatch(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)
    original = _refreshable_challenge(project, task_id)
    store = Store(cli.db_path(project))
    database = connected_dispatch_database(store.conn)
    assert database is not None

    with stage_dispatch_lock(database, task_id).hold("test-stage"):
        code, refusal = _refresh_challenge(project, task_id, capsys)

    store.close()
    assert code == 2 and refusal["code"] == "stage_dispatch_active"
    assert (project / ".stagemesh" / "planning" / f"{task_id}.challenge.json").read_bytes() == original


def test_refresh_planning_challenge_refuses_unsafe_history_path(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)
    original = _refreshable_challenge(project, task_id)
    real_check = spec_planning._is_link_or_junction
    monkeypatch.setattr(
        spec_planning,
        "_is_link_or_junction",
        lambda path: path.name == "history" or real_check(path),
    )

    code, refusal = _refresh_challenge(project, task_id, capsys)

    assert code == 2 and refusal["code"] == "unsafe_history_path"
    assert (project / ".stagemesh" / "planning" / f"{task_id}.challenge.json").read_bytes() == original


@pytest.mark.parametrize("moved", [False, True])
def test_interrupted_refresh_intent_is_reconciled_without_losing_artifact(tmp_path, moved):
    project = tmp_path / "project"
    task_id = _approval_project(project)
    _refreshable_challenge(project, task_id)
    challenge_path = project / ".stagemesh" / "planning" / f"{task_id}.challenge.json"
    digest = freeze_planning_challenge(json.loads(challenge_path.read_text(encoding="utf-8"))).digest
    history = challenge_path.parent / "history"
    history.mkdir()
    archive = history / f"{challenge_path.stem}.{digest}.interrupted.json"
    payload = {
        "task_id": task_id,
        "artifact_digest": digest,
        "verdict": "needs-human-decision",
        "reason": "simulate an interrupted operator process",
        "archive_path": archive.relative_to(project).as_posix(),
    }
    store = Store(cli.db_path(project))
    intent_id = store.add_audit_event(PLANNING_CHALLENGE_RETIRE_INTENT_EVENT, payload)
    if moved:
        challenge_path.replace(archive)

    assert len(reconcile_planning_challenge_retirement(store, project, task_id)) == 1

    event_type = PLANNING_CHALLENGE_RETIRED_EVENT if moved else PLANNING_CHALLENGE_RETIRE_ABORTED_EVENT
    event = list(store.task_audit_events(event_type, task_id))[-1]
    assert json.loads(event["payload"])["intent_event_id"] == intent_id
    assert archive.exists() is moved
    assert challenge_path.exists() is (not moved)
    store.close()


@pytest.mark.parametrize("previously_moved", [False, True])
def test_refresh_retry_reconciles_prior_open_intent_before_starting_again(
    tmp_path, monkeypatch, capsys, previously_moved
):
    monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS", raising=False)
    project = tmp_path / "project"
    task_id = _approval_project(project)
    _refreshable_challenge(project, task_id)
    challenge_path = project / ".stagemesh" / "planning" / f"{task_id}.challenge.json"
    digest = freeze_planning_challenge(json.loads(challenge_path.read_text(encoding="utf-8"))).digest
    history = challenge_path.parent / "history"
    history.mkdir()
    archive = history / f"{challenge_path.stem}.{digest}.prior-intent.json"
    store = Store(cli.db_path(project))
    store.add_audit_event(
        PLANNING_CHALLENGE_RETIRE_INTENT_EVENT,
        {
            "task_id": task_id,
            "artifact_digest": digest,
            "verdict": "needs-human-decision",
            "reason": "prior interrupted command",
            "archive_path": archive.relative_to(project).as_posix(),
        },
    )
    store.close()
    if previously_moved:
        challenge_path.replace(archive)

    code, report = _refresh_challenge(project, task_id, capsys, reason="retry after interruption")

    assert code == 0 and report["retired_challenge_hash"] == digest
    store = Store(cli.db_path(project))
    try:
        assert reconcile_planning_challenge_retirement(store, project, task_id) == ()
        assert list(store.task_audit_events(PLANNING_CHALLENGE_RETIRED_EVENT, task_id))
        if not previously_moved:
            assert list(store.task_audit_events(PLANNING_CHALLENGE_RETIRE_ABORTED_EVENT, task_id))
    finally:
        store.close()
