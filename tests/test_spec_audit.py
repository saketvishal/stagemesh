from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stagemesh.cli import main
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.git import GitWorkspace
from stagemesh.spec_integrity import OpenSpecAdapter, run_openspec_validation, spec_audit_report


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _project(tmp_path: Path, config: dict[str, object]) -> tuple[Path, str]:
    project = tmp_path / "project"
    project.mkdir()
    _write(project, ".stagemesh/config.json", json.dumps(config))
    _write(project, "openspec/specs/auth/spec.md", "### Requirement: Login\nUsers MUST log in.\n")
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "Test Owner")
    revision = git.commit_all("spec baseline")
    return project, revision


def _fake_validator(tmp_path: Path, validation_output: str, *, version: str = "1.13.2", exit_code: int = 0) -> Path:
    script = tmp_path / "fake_openspec.py"
    output_statement = (
        "print(json.dumps({'valid': True, 'syncStatus': 'in_sync', "
        "'credentialPresent': bool(os.environ.get('OPENAI_API_KEY'))}))"
        if validation_output == "__ENV__"
        else f"print({validation_output!r})"
    )
    script.write_text(
        "import json, os, sys\n"
        "if '--version' in sys.argv:\n"
        f"    print('OpenSpec {version}')\n"
        "    raise SystemExit(0)\n"
        f"{output_statement}\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    return script


def test_spec_governance_config_requires_exact_pins_and_safe_argument_list(tmp_path: Path) -> None:
    project, _ = _project(
        tmp_path,
        {
            "spec_governance": {
                "adapter": "openspec",
                "version": "layout-v1",
                "openspec_validation": {
                    "command": [sys.executable, "tools/openspec.py"],
                    "expected_version": "1.13.2",
                    "timeout_seconds": 15,
                },
            }
        },
    )
    policy = load_config(project).spec_governance
    assert policy is not None and policy.openspec_validation is not None
    assert policy.openspec_validation.command == (sys.executable, "tools/openspec.py")
    assert policy.openspec_validation.expected_version == "1.13.2"

    for invalid in (
        {"adapter": "openspec"},
        {"adapter": "openspec", "version": "layout-v1", "openspec_validation": {"command": "openspec"}},
        {
            "adapter": "openspec",
            "version": "layout-v1",
            "openspec_validation": {"command": ["openspec"], "expected_version": "latest"},
        },
        {
            "adapter": "openspec",
            "version": "layout-v1",
            "openspec_validation": {"command": ["open\u0000spec"], "expected_version": "1.13.2"},
        },
        {"adapter": "custom-json", "version": "1"},
    ):
        _write(project, ".stagemesh/config.json", json.dumps({"spec_governance": invalid}))
        with pytest.raises(ConfigValidationError):
            load_config(project)


def test_spec_risk_policy_is_strict_deterministic_and_independent_review_compatible(
    tmp_path: Path,
) -> None:
    risk_classes = {
        "sensitive": {
            "required_owner_roles": ["security-owner", "product-owner"],
            "required_validation_gates": ["security-tests"],
            "require_independent_review": True,
        },
        "ordinary": {
            "required_owner_roles": ["product-owner"],
            "required_validation_gates": [],
            "require_independent_review": False,
        },
    }
    project, _ = _project(
        tmp_path,
        {
            "spec_governance": {
                "adapter": "openspec",
                "version": "layout-v1",
                "risk_classes": risk_classes,
            }
        },
    )
    policy = load_config(project).spec_governance
    assert policy is not None
    assert policy.risk_classes["sensitive"].required_owner_roles == (
        "security-owner", "product-owner",
    )
    fingerprint = policy.risk_policy_fingerprint
    assert fingerprint is not None and fingerprint.startswith("sha256:")

    reordered = {
        "ordinary": risk_classes["ordinary"],
        "sensitive": {
            **risk_classes["sensitive"],
            "required_owner_roles": ["product-owner", "security-owner"],
        },
    }
    _write(project, ".stagemesh/config.json", json.dumps({
        "spec_governance": {
            "adapter": "openspec", "version": "layout-v1", "risk_classes": reordered,
        }
    }))
    assert load_config(project).spec_governance.risk_policy_fingerprint == fingerprint  # type: ignore[union-attr]

    invalid_configs = (
        {
            "routing": {"require_independent_review": False},
            "spec_governance": {
                "adapter": "openspec", "version": "layout-v1",
                "risk_classes": {"sensitive": risk_classes["sensitive"]},
            },
        },
        {
            "spec_governance": {
                "adapter": "openspec", "version": "layout-v1",
                "risk_classes": {"sensitive": {
                    "required_owner_roles": ["security-owner", "security-owner"]
                }},
            }
        },
        {
            "spec_governance": {
                "adapter": "openspec", "version": "layout-v1",
                "risk_classes": {"Sensitive Class": {"required_owner_roles": ["owner"]}},
            }
        },
        {
            "spec_governance": {
                "adapter": "openspec", "version": "layout-v1",
                "risk_classes": {"ordinary": {"required_owner_roles": [], "unknown": True}},
            }
        },
    )
    for invalid in invalid_configs:
        _write(project, ".stagemesh/config.json", json.dumps(invalid))
        with pytest.raises(ConfigValidationError):
            load_config(project)


def test_read_only_audit_distinguishes_authority_pending_and_structural_failures(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project,
        "openspec/specs/auth/spec.md",
        "### Requirement: Login\nUsers MUST log in. [route](../routes/spec.md#requirement-route)\n",
    )
    _write(project, "openspec/changes/archive/old/specs/auth/spec.md", "### Requirement: Old\nHistory only.\n")
    _write(project, "openspec/changes/new-login/specs/auth/spec.md", "### Requirement: Pending\nNot authority.\n")

    report = spec_audit_report(project, OpenSpecAdapter(), project_revision="a" * 40)

    assert report["status"] == "blocked"
    assert report["counts"] == {"active_owners": 1, "historical_owners": 1, "pending_changes": 1}
    assert report["pending_changes"] == ["openspec/changes/new-login"]
    assert any("unresolved reference" in item for item in report["blocking_findings"])
    assert any("pending OpenSpec" in item for item in report["review_findings"])
    assert report["read_only"] is True and report["assessment"] == "structural-only"


def test_configured_openspec_validator_is_fixed_redacted_bounded_and_secret_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-do-not-forward")
    script = _fake_validator(project, "__ENV__")

    evidence = run_openspec_validation(
        project,
        (sys.executable, str(script)),
        expected_version="1.13.2",
        timeout_seconds=10,
    )

    assert evidence.ok and evidence.observed_version == "1.13.2"
    assert evidence.resolved_executable == str(Path(sys.executable).resolve())
    assert evidence.sync_status == "in_sync"
    assert evidence.validation_command[-5:] == (
        "validate", "--all", "--strict", "--json", "--no-interactive",
    )
    assert '"credentialPresent": false' in evidence.stdout_excerpt
    assert "sk-do-not-forward" not in json.dumps(evidence.to_dict())


def test_version_mismatch_stops_before_validation_and_zero_exit_cannot_hide_out_of_sync(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    script = _fake_validator(project, json.dumps({"valid": True, "syncStatus": "in_sync"}), version="1.13.1")
    mismatch = run_openspec_validation(project, (sys.executable, str(script)), expected_version="1.13.2")
    assert not mismatch.ok and "exact pin" in mismatch.errors[0]
    assert mismatch.validation_command == ()

    script = _fake_validator(project, json.dumps({"valid": True, "syncStatus": "out_of_sync"}))
    stale = run_openspec_validation(project, (sys.executable, str(script)), expected_version="1.13.2")
    assert not stale.ok and stale.exit_code == 0
    assert stale.sync_status == "out_of_sync"
    assert any("sync status" in item for item in stale.errors)


def test_validator_missing_timeout_nonzero_and_secret_output_fail_or_redact(tmp_path: Path) -> None:
    invalid_policy = run_openspec_validation(
        tmp_path,
        ("openspec",),
        expected_version="latest",
        timeout_seconds=0,
    )
    assert not invalid_policy.ok and any("exact semantic version" in item for item in invalid_policy.errors)

    malformed = run_openspec_validation(
        tmp_path,
        ("openspec\nmalicious",),
        expected_version="1.13.2",
    )
    assert not malformed.ok and any("bounded" in item for item in malformed.errors)

    missing = run_openspec_validation(
        tmp_path,
        (str(tmp_path / "missing-openspec"),),
        expected_version="1.13.2",
        timeout_seconds=1,
    )
    assert not missing.ok and any("could not be resolved" in item for item in missing.errors)

    slow = tmp_path / "slow.py"
    slow.write_text(
        "import sys, time\n"
        "if '--version' in sys.argv: print('1.13.2')\n"
        "else: time.sleep(2); print('{}')\n",
        encoding="utf-8",
    )
    timed_out = run_openspec_validation(
        tmp_path,
        (sys.executable, str(slow)),
        expected_version="1.13.2",
        timeout_seconds=0.2,
    )
    assert not timed_out.ok and any("timed out" in item for item in timed_out.errors)

    failed_script = _fake_validator(tmp_path, json.dumps({"valid": False}), exit_code=2)
    failed = run_openspec_validation(
        tmp_path,
        (sys.executable, str(failed_script)),
        expected_version="1.13.2",
    )
    assert not failed.ok and any("exited 2" in item for item in failed.errors)

    secret_script = _fake_validator(
        tmp_path,
        json.dumps({"valid": True, "token": "validator-secret-value"}),
    )
    redacted = run_openspec_validation(
        tmp_path,
        (sys.executable, str(secret_script)),
        expected_version="1.13.2",
    )
    assert redacted.ok
    assert "validator-secret-value" not in redacted.stdout_excerpt
    assert "***REDACTED***" in redacted.stdout_excerpt


@pytest.mark.parametrize("output", ["not-json", "[]", "null"])
def test_validator_malformed_output_fails_closed(tmp_path: Path, output: str) -> None:
    project = tmp_path / output.replace("-", "_").replace("[]", "list")
    project.mkdir()
    script = _fake_validator(project, output)
    evidence = run_openspec_validation(project, (sys.executable, str(script)), expected_version="1.13.2")
    assert not evidence.ok
    assert any("JSON" in item for item in evidence.errors)


def test_validator_output_evidence_is_bounded(tmp_path: Path) -> None:
    script = _fake_validator(tmp_path, json.dumps({"valid": True, "detail": "x" * 5000}))
    evidence = run_openspec_validation(
        tmp_path,
        (sys.executable, str(script)),
        expected_version="1.13.2",
    )
    assert evidence.ok
    assert len(evidence.stdout_excerpt) == 2000
    assert len(evidence.stderr_excerpt) <= 2000


def test_spec_audit_cli_is_idempotent_json_and_text_and_uses_blocking_exit_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, revision = _project(
        tmp_path,
        {"spec_governance": {
            "adapter": "openspec", "version": "layout-v1",
            "risk_classes": {"ordinary": {
                "required_owner_roles": ["product-owner"],
                "required_validation_gates": [],
            }},
        }},
    )
    before = {path.relative_to(project).as_posix(): path.read_bytes() for path in project.rglob("*") if path.is_file()}

    assert main(["--project", str(project), "spec-audit", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project_revision"] == revision
    assert payload["status"] == "passed"
    assert payload["openspec_validation"] is None
    assert payload["risk_policy"]["classes"] == ["ordinary"]
    assert payload["risk_policy"]["fingerprint"].startswith("sha256:")
    assert main(["--project", str(project), "spec-audit"]) == 0
    text_report = capsys.readouterr().out
    assert "spec governance audit: PASSED" in text_report
    assert "risk policy: sha256:" in text_report
    after = {path.relative_to(project).as_posix(): path.read_bytes() for path in project.rglob("*") if path.is_file()}
    assert after == before

    _write(
        project,
        "openspec/specs/auth/spec.md",
        "### Requirement: Login\nUsers MUST log in. [missing](../missing/spec.md)\n",
    )
    assert main(["--project", str(project), "spec-audit", "--json"]) == 1
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "blocked"


def test_spec_audit_cli_supports_declarative_custom_sdd_authority(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "custom"
    project.mkdir()
    _write(
        project,
        ".stagemesh/config.json",
        json.dumps(
            {
                "spec_governance": {
                    "adapter": "custom-json",
                    "version": "1",
                    "contract_path": "docs/spec-authority.json",
                }
            }
        ),
    )
    _write(project, "docs/current.md", "Current product requirement.\n")
    _write(project, "docs/history.md", "Historical product requirement.\n")
    _write(
        project,
        "docs/spec-authority.json",
        json.dumps(
            {
                "schema_version": 1,
                "owners": [
                    {
                        "id": "current",
                        "path": "docs/current.md",
                        "status": "active",
                        "dependencies": [],
                        "requirements": [{"id": "REQ-1", "text": "Current", "dependencies": []}],
                    },
                    {
                        "id": "old",
                        "path": "docs/history.md",
                        "status": "history",
                        "dependencies": [],
                        "requirements": [{"id": "OLD-1", "text": "Historical", "dependencies": []}],
                    },
                ],
            }
        ),
    )
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "Test Owner")
    git.commit_all("custom authority")

    assert main(["--project", str(project), "spec-audit", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["adapter"] == {"name": "custom-json", "version": "1"}
    assert report["counts"] == {"active_owners": 1, "historical_owners": 1, "pending_changes": 0}
