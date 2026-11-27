import json
import sys
from pathlib import Path

import pytest
from test_run_ready import _project, _run

from stagemesh.cli import _configure_governance_profile, main
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.persistence import Store

AUTHORITY = {
    "version": 1,
    "authority_id": "project-owner:example",
    "approver_id": "owner:example",
}


def _write(project: Path, **overrides: object) -> None:
    data: dict[str, object] = {
        "governance_profile": "governed-sdd",
        "providers": {"planner": "planner", "challenger": "challenger"},
        "authorization": {"unattended_providers": ["planner", "challenger"]},
        "routing": {"require_independent_review": True},
        "planning_gate": {
            "planner_provider": "planner",
            "challenger_provider": "challenger",
            "approval_authority": AUTHORITY,
        },
        "merging": {"mode": "pr_only"},
    }
    data.update(overrides)
    runtime = project / ".stagemesh"
    runtime.mkdir()
    (runtime / "config.json").write_text(json.dumps(data), encoding="utf-8")


def test_governed_sdd_profile_enables_all_required_planning_gates(tmp_path: Path) -> None:
    _write(tmp_path)

    config = load_config(tmp_path)

    assert config.governance_profile == "governed-sdd"
    assert config.require_approved_spec is True
    assert config.require_plan_challenge is True
    assert config.require_independent_review is True
    assert config.planner_provider == "planner"
    assert config.challenger_provider == "challenger"
    assert config.approval_authority is not None
    assert config.merging.mode == "pr_only"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"routing": {"require_independent_review": False}}, "independent_review"),
        ({"planning_gate": {"planner_provider": "planner", "challenger_provider": "challenger", "approval_authority": AUTHORITY, "require_approved_spec": False}}, "require_approved_spec"),
        ({"planning_gate": {"planner_provider": "planner", "challenger_provider": "challenger", "approval_authority": AUTHORITY, "require_plan_challenge": False}}, "require_plan_challenge"),
        ({"planning_gate": {"approval_authority": AUTHORITY}}, "planner_provider and challenger_provider"),
        ({"planning_gate": {"planner_provider": "planner", "challenger_provider": "challenger"}}, "approval_authority"),
        ({"merging": {"mode": "hosted", "required_checks": ["ci"]}}, "merging.mode=pr_only"),
        ({"authorization": {"unattended_providers": ["planner"]}}, "authorized planning providers"),
    ],
)
def test_governed_sdd_profile_cannot_be_weakened(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    _write(tmp_path, **overrides)

    with pytest.raises(ConfigValidationError, match=message):
        load_config(tmp_path)


def test_legacy_projects_remain_opt_in(tmp_path: Path) -> None:
    config = load_config(tmp_path)

    assert config.governance_profile is None
    assert config.require_approved_spec is False
    assert config.require_plan_challenge is False


def test_init_configures_governed_profile_from_authorized_installed_providers(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / ".stagemesh"
    runtime.mkdir()
    (runtime / "config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "codex": "codex",
                    "claude": "claude",
                    "offline": "offline",
                    "implementation-only": {"command": "worker", "capabilities": ["IMPLEMENT"]},
                },
                "authorization": {"unattended_providers": ["codex", "claude", "offline", "implementation-only"]},
            }
        ),
        encoding="utf-8",
    )

    changed = _configure_governance_profile(
        tmp_path,
        "governed-sdd",
        planner_provider=None,
        challenger_provider=None,
        authority_id="project-owner:example",
        approver_id="github:owner",
        available_providers=("implementation-only", "claude", "codex"),
    )

    config = load_config(tmp_path)
    assert changed is True
    assert (config.planner_provider, config.challenger_provider) == ("claude", "codex")
    assert config.approval_authority is not None
    assert config.approval_authority.approver_id == "github:owner"
    assert _configure_governance_profile(
        tmp_path,
        "governed-sdd",
        planner_provider=None,
        challenger_provider=None,
        authority_id=None,
        approver_id=None,
        available_providers=("claude", "codex"),
    ) is False


def test_init_refuses_an_implementation_only_challenger_without_writing(tmp_path: Path) -> None:
    _write(tmp_path, providers={
        "planner": "planner",
        "challenger": {"command": "worker", "capabilities": ["IMPLEMENT"]},
    })
    config_path = tmp_path / ".stagemesh" / "config.json"
    before = config_path.read_bytes()
    with pytest.raises(ConfigValidationError, match="configured REVIEW command: challenger"):
        _configure_governance_profile(
            tmp_path, "governed-sdd", planner_provider=None, challenger_provider=None,
            authority_id=None, approver_id=None, available_providers=("planner", "challenger"),
        )
    assert config_path.read_bytes() == before


def test_init_never_infers_accountable_authority(tmp_path: Path) -> None:
    runtime = tmp_path / ".stagemesh"
    runtime.mkdir()
    (runtime / "config.json").write_text(
        json.dumps(
            {
                "providers": {"codex": "codex", "claude": "claude"},
                "authorization": {"unattended_providers": ["codex", "claude"]},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="accountable identity"):
        _configure_governance_profile(
            tmp_path,
            "governed-sdd",
            planner_provider=None,
            challenger_provider=None,
            authority_id=None,
            approver_id=None,
            available_providers=("claude", "codex"),
        )


def test_init_rejects_unavailable_explicit_governance_provider_without_writing(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / ".stagemesh"
    runtime.mkdir()
    config_path = runtime / "config.json"
    original = json.dumps(
        {
            "providers": {"codex": "codex", "claude": "claude"},
            "authorization": {"unattended_providers": ["codex", "claude"]},
        }
    )
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigValidationError, match="installed and authorized"):
        _configure_governance_profile(
            tmp_path,
            "governed-sdd",
            planner_provider="codex",
            challenger_provider="claude",
            authority_id="project-owner:example",
            approver_id="github:owner",
            available_providers=("codex",),
        )

    assert config_path.read_text(encoding="utf-8") == original


def test_governed_init_canary_refuses_before_any_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("stagemesh.config.detect_github_repository", lambda _project: None)
    monkeypatch.setattr(
        "stagemesh.cli._ensure_provider_registry",
        lambda _project: (
            False,
            ("planner", "challenger"),
            ("planner", "challenger"),
            (),
        ),
    )
    project = _project(
        tmp_path,
        ["T-1"],
        config={
            "providers": {"planner": sys.executable, "challenger": sys.executable},
            "authorization": {"unattended_providers": ["planner", "challenger"]},
        },
    )

    code = main(
        [
            "--project",
            str(project),
            "init",
            "--allow-incomplete",
            "--governance-profile",
            "governed-sdd",
            "--planner-provider",
            "planner",
            "--challenger-provider",
            "challenger",
            "--approval-authority-id",
            "project-owner:example",
            "--approver-id",
            "github:owner",
        ]
    )
    capsys.readouterr()

    assert code == 0
    config = load_config(project)
    assert config.governance_profile == "governed-sdd"
    assert config.merging.mode == "pr_only"
    run_code, result = _run(project)
    assert run_code != 0
    assert result["stop_reason"] == "REFUSED:planning_execution"

    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        provider_calls = store.conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='provider.call'"
        ).fetchone()[0]
    finally:
        store.close()
    assert provider_calls == 0
