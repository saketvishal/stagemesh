from __future__ import annotations

import json
from pathlib import Path

from stagemesh.change_control import (
    ChangeContract,
    DiffSummary,
    contract_violations,
    write_contract,
)


def test_contract_rejects_out_of_scope_and_dependency_changes() -> None:
    contract = ChangeContract.from_mapping(
        {
            "objective": "fix coordinator",
            "allowed_paths": ["src/stagemesh/*.py", "tests/*.py"],
            "forbidden_paths": ["src/stagemesh/security.py"],
            "max_changed_files": 4,
            "max_changed_lines": 100,
        }
    )
    summary = DiffSummary(
        (
            "src/stagemesh/coordinator.py",
            "src/stagemesh/security.py",
            "pyproject.toml",
        ),
        12,
    )
    violations = contract_violations(contract, summary)
    assert "forbidden path changed: src/stagemesh/security.py" in violations
    assert "out-of-scope path changed: pyproject.toml" in violations
    assert "dependency manifest changed without permission: pyproject.toml" in violations


def test_single_star_does_not_cross_repository_directories() -> None:
    contract = ChangeContract.from_mapping(
        {
            "objective": "change only direct python children",
            "allowed_paths": ["src/*.py"],
        }
    )
    violations = contract_violations(
        contract,
        DiffSummary(("src/nested/file.py",), 1),
    )
    assert violations == ["out-of-scope path changed: src/nested/file.py"]


def test_contract_requires_expected_changed_path() -> None:
    contract = ChangeContract.from_mapping(
        {
            "objective": "add regression test",
            "required_changed_paths": ["tests/test_regression.py"],
        }
    )
    violations = contract_violations(contract, DiffSummary(("src/stagemesh/coordinator.py",), 5))
    assert violations == ["required path was not changed: tests/test_regression.py"]


def test_contract_round_trip(tmp_path: Path) -> None:
    contract = ChangeContract.from_mapping(
        {
            "objective": "bounded change",
            "acceptance_criteria": ["test passes"],
            "validation_commands": ["python -m pytest -q"],
            "allow_dependency_changes": False,
        }
    )
    path = write_contract(tmp_path, "TASK-1", contract)
    assert path.exists()
    loaded = ChangeContract.load(tmp_path, "TASK-1")
    assert loaded == contract
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["objective"] == "bounded change"
