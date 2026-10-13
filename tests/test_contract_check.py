from __future__ import annotations

import json
from pathlib import Path

from stagemesh.contract_check import check_contract, check_contract_text, format_report

VALID = {"objective": "Do the thing", "explicit": True, "required_tests": [{"name": "unit", "command": "pytest -q"}]}


def test_valid_contract_reports_size_limit_and_gates() -> None:
    report = check_contract_text(json.dumps(VALID))
    assert report["ok"] and report["errors"] == []
    assert report["size"] <= report["limit"] and report["gates"]
    assert report["largest_sections"][0]["chars"] >= report["largest_sections"][-1]["chars"]


def test_invalid_contracts_fail_closed() -> None:
    assert not check_contract_text("{not json")["ok"]
    report = check_contract_text(json.dumps({"explicit": True}))
    assert not report["ok"] and report["size"] is None and "invalid" in report["errors"][0]


def test_oversized_contract_names_size_limit_and_largest_section() -> None:
    report = check_contract_text(json.dumps(dict(VALID, objective="x" * 11000)))
    assert not report["ok"]
    assert report["size"] > report["limit"] == 10000
    assert report["largest_sections"][0]["section"] == "objective"
    assert "objective (" in report["errors"][0] and "limit is 10000" in report["errors"][0]
    assert any("objective" in line for line in format_report(report))


def test_check_contract_by_task_and_missing_file(tmp_path: Path) -> None:
    contracts = tmp_path / ".stagemesh" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "T-1.json").write_text(json.dumps(VALID), encoding="utf-8")
    assert check_contract(tmp_path, "T-1")["ok"]
    missing = check_contract(tmp_path, "T-2")
    assert not missing["ok"] and "no contract file" in missing["errors"][0]


def test_require_spec_fails_without_spec_ref() -> None:
    report = check_contract_text(json.dumps(VALID), require_spec=True)

    assert not report["ok"]
    assert "requires at least one spec_refs" in report["errors"][0]


def test_require_spec_checks_referenced_file_exists_and_has_content(tmp_path: Path) -> None:
    contracts = tmp_path / ".stagemesh" / "contracts"
    docs = tmp_path / "docs"
    contracts.mkdir(parents=True)
    docs.mkdir()
    payload = dict(VALID, spec_refs=["docs/spec.md"])
    (contracts / "T-1.json").write_text(json.dumps(payload), encoding="utf-8")

    missing = check_contract(tmp_path, "T-1", require_spec=True)
    assert not missing["ok"] and "docs/spec.md" in missing["errors"][0]

    (docs / "spec.md").write_text("Strict PRD\n", encoding="utf-8")
    assert check_contract(tmp_path, "T-1", require_spec=True)["ok"]
