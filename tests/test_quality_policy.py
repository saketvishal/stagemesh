from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stagemesh.contracts import ChangeContract, GateCommand, evaluate_contract
from stagemesh.git import GitWorkspace
from stagemesh.quality_policy import (
    POLICY_PATH,
    QualityPolicyError,
    apply_quality_policy,
    load_quality_policy,
    parse_quality_policy,
    quality_scope_findings,
)

ROOT = Path(__file__).resolve().parents[1]


def _payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "policy_id": "test/1",
        "complexity_max": 12,
        "dead_code_min_confidence": 100,
        "strict_modules": ["src/typed.py"],
        "authorization_required": ["pyproject.toml", ".github/**"],
        "gates": [
            {
                "name": "lint",
                "category": "lint",
                "command": ["python", "-m", "ruff", "check", "."],
                "applies_to": ["**/*.py"],
            },
            {
                "name": "types",
                "category": "typecheck",
                "command": ["python", "-m", "mypy", "--strict", "src/typed.py"],
                "applies_to": ["src/**/*.py"],
            },
        ],
    }


def _write_policy(project: Path, payload: dict[str, object]) -> None:
    path = project / POLICY_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_repository_policy_is_versioned_and_has_all_required_gate_classes() -> None:
    import build_backend

    policy = load_quality_policy(ROOT)
    assert policy is not None
    assert policy.policy_id == "stagemesh-repository-quality/1"
    assert len(policy.digest) == 64
    assert {gate.category for gate in policy.gates}.issuperset({"lint", "typecheck", "test"})
    assert policy.complexity_max == 12
    assert policy.dead_code_min_confidence == 100
    metadata = build_backend._metadata()
    assert 'Requires-Dist: mypy>=1.13,<2; extra == "dev"' in metadata
    assert 'Requires-Dist: vulture>=2.14,<3; extra == "dev"' in metadata


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"complexity_max": 21}, "complexity_max"),
        ({"dead_code_min_confidence": 59}, "dead_code_min_confidence"),
        ({"strict_modules": []}, "strict_modules"),
        ({"gates": []}, "gates"),
    ],
)
def test_policy_parser_fails_closed_on_missing_or_weakened_controls(mutation, message) -> None:
    payload = _payload()
    payload.update(mutation)
    with pytest.raises(QualityPolicyError, match=message):
        parse_quality_policy(payload)


def test_policy_rejects_shell_strings_unsafe_working_directories_and_duplicate_names() -> None:
    payload = _payload()
    gates = list(payload["gates"])
    gates[0] = {**gates[0], "command": "ruff check ."}
    payload["gates"] = gates
    with pytest.raises(QualityPolicyError, match="command"):
        parse_quality_policy(payload)

    payload = _payload()
    gates = list(payload["gates"])
    gates[0] = {**gates[0], "cwd": "../outside"}
    payload["gates"] = gates
    with pytest.raises(QualityPolicyError, match="inside"):
        parse_quality_policy(payload)

    payload = _payload()
    gates = list(payload["gates"])
    gates[1] = {**gates[1], "name": gates[0]["name"]}
    payload["gates"] = gates
    with pytest.raises(QualityPolicyError, match="unique"):
        parse_quality_policy(payload)


def test_trusted_policy_adds_exact_commit_gates_and_policy_hash_to_fingerprint_inputs(tmp_path: Path) -> None:
    _write_policy(tmp_path, _payload())
    contract = ChangeContract("change", explicit=True, allowed_files=("src/typed.py",))
    updated, applied = apply_quality_policy(
        tmp_path,
        contract,
        ("src/typed.py",),
        baseline_sha="a" * 40,
        candidate_sha="b" * 40,
    )
    assert applied is not None
    assert [gate.name for gate in updated.lint] == ["lint"]
    assert [gate.name for gate in updated.typecheck] == ["types"]
    environment = dict(updated.lint[0].env)
    assert environment == {
        "STAGEMESH_QUALITY_BASE_SHA": "a" * 40,
        "STAGEMESH_QUALITY_HEAD_SHA": "b" * 40,
        "STAGEMESH_QUALITY_POLICY_SHA256": applied.policy.digest,
    }
    assert applied.evidence()["trust_boundary"] == "loaded from integration checkout, not candidate"


def test_policy_gate_name_cannot_override_a_different_task_command(tmp_path: Path) -> None:
    _write_policy(tmp_path, _payload())
    contract = ChangeContract(
        "change",
        explicit=True,
        allowed_files=("src/typed.py",),
        lint=(GateCommand("lint", ["python", "-c", "pass"]),),
    )
    with pytest.raises(QualityPolicyError, match="conflicts"):
        apply_quality_policy(
            tmp_path,
            contract,
            ("src/typed.py",),
            baseline_sha="a" * 40,
            candidate_sha="b" * 40,
        )


def test_sensitive_scope_requires_exact_explicit_authorization() -> None:
    policy = parse_quality_policy(_payload())
    broad = ChangeContract("broad", explicit=True, allowed_files=("**",))
    findings = quality_scope_findings(policy, broad, ("pyproject.toml",))
    assert findings[0]["code"] == "quality_scope_authorization_missing"

    exact = ChangeContract("exact", explicit=True, allowed_files=("pyproject.toml",))
    assert quality_scope_findings(policy, exact, ("pyproject.toml",)) == ()


def test_missing_policy_is_opt_in_for_non_python_managed_repositories(tmp_path: Path) -> None:
    contract = ChangeContract("non-python", explicit=True, allowed_files=("app/main.go",))
    updated, applied = apply_quality_policy(
        tmp_path,
        contract,
        ("app/main.go",),
        baseline_sha="a" * 40,
        candidate_sha="b" * 40,
    )
    assert updated == contract
    assert applied is None


def test_mixed_python_typescript_policy_blocks_a_failing_language_gate(tmp_path: Path) -> None:
    project = tmp_path / "mixed"
    project.mkdir()
    workspace = GitWorkspace(project)
    workspace.run("init", "-b", "main")
    workspace.run("config", "user.name", "Test")
    workspace.run("config", "user.email", "test@example.com")
    (project / "src").mkdir()
    (project / "web").mkdir()
    (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "web" / "app.ts").write_text("export const value = 1;\n", encoding="utf-8")
    payload = _payload()
    payload["gates"] = [
        {
            "name": "python-lint",
            "category": "lint",
            "command": [sys.executable, "-c", "pass"],
            "applies_to": ["**/*.py"],
        },
        {
            "name": "python-types",
            "category": "typecheck",
            "command": [sys.executable, "-c", "pass"],
            "applies_to": ["**/*.py"],
        },
        {
            "name": "typescript-lint",
            "category": "lint",
            "command": [sys.executable, "-c", "raise SystemExit(7)"],
            "applies_to": ["**/*.ts"],
        },
        {
            "name": "typescript-types",
            "category": "typecheck",
            "command": [sys.executable, "-c", "pass"],
            "applies_to": ["**/*.ts"],
        },
    ]
    _write_policy(project, payload)
    workspace.run("add", "-A")
    workspace.run("commit", "-m", "mixed baseline")
    baseline = workspace.head()
    (project / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (project / "web" / "app.ts").write_text("export const value = 2;\n", encoding="utf-8")
    workspace.run("add", "-A")
    workspace.run("commit", "-m", "mixed candidate")
    candidate = workspace.head()
    contract = ChangeContract(
        "mixed change",
        explicit=True,
        allowed_files=("src/app.py", "web/app.ts"),
    )

    result = evaluate_contract(project, candidate, contract, baseline_sha=baseline)

    gates = {gate.name: gate for gate in result.gates}
    assert set(gates) == {"python-lint", "python-types", "typescript-lint", "typescript-types"}
    assert gates["typescript-lint"].status == "FAILED"
    assert gates["typescript-lint"].returncode == 7
    assert result.status == "FAILED"
