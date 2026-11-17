from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from stagemesh import api_server, cli, ui_launcher
from stagemesh.persistence import Store
from stagemesh.registry import GlobalRegistry, ProjectRegistration


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
