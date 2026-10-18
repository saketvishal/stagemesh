"""Scenario 4: a mandatory requirement outside the contract's allowed files is found before any provider runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stagemesh.contract_check import check_contract
from stagemesh.contract_binding import ContractRejected, bind_task_contract
from stagemesh.contract_feasibility import check_contract_feasibility, patterns_overlap
from stagemesh.contracts import parse_contract
from stagemesh.persistence import Store
from stagemesh.run_ready import RunReadyRefusal, _ensure_contract
from stagemesh.task_selection import contract_state

from test_bounded_execution import TASK, _setup

TWO_TESTS = {
    "objective": "Add regression coverage for the retry cap",
    "allowed_files": ["tests/test_retry_cap.py", "tests/test_retry_cap_windows.py"],
    "required_tests": [{"name": "unit", "command": ["python", "-c", "pass"]}],
}


def _policy(project: Path, **requirement) -> None:
    folder = project / ".stagemesh" / "policy"
    folder.mkdir(parents=True, exist_ok=True)
    item = {
        "id": "engineering-impact",
        "kind": "documentation",
        "description": "Engineering Impact record",
        "paths": ["docs/design/**"],
        **requirement,
    }
    (folder / "required-changes.json").write_text(json.dumps({"version": 1, "requirements": [item]}), encoding="utf-8")


def test_structured_policy_requirement_outside_allowed_files_is_one_precise_blocker(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, TWO_TESTS)
    _policy(project)

    report = check_contract_feasibility(project, parse_contract(TWO_TESTS))

    assert not report.feasible and len(report.conflicts) == 1
    message = report.message(TASK, parse_contract(TWO_TESTS))
    assert "Engineering Impact record" in message and "docs/design/**" in message
    assert "none of those paths is within allowed_files" in message
    assert "rebind-contract" in message and "prerequisite task" in message and "policy owner" in message
    assert "did not widen the contract" in message  # nothing was widened, relocated or created automatically


def test_prose_policy_in_agents_md_is_detected_when_unconditional(tmp_path: Path) -> None:
    project, _ = _setup(tmp_path, TWO_TESTS)
    (project / "AGENTS.md").write_text(
        "# Rules\n\nEvery change must add an Engineering Impact record to the matching design file under `docs/design/`.\n"
        "When a public API changes, you must update `docs/api.md`.\n"
        "Do not update `LICENSE` by hand.\n",
        encoding="utf-8",
    )

    report = check_contract_feasibility(project, parse_contract(TWO_TESTS))

    assert [c.requirement.paths for c in report.conflicts] == [("docs/design/**",)]
    assert report.conflicts[0].requirement.origin == "policy-prose" and report.conflicts[0].requirement.source == "AGENTS.md:3"
    assert [a.paths for a in report.advisories] == [("docs/api.md",)]  # conditional: reported, never blocking
    # a negated sentence is not a requirement at all


def test_the_contracts_own_acceptance_text_can_contradict_its_allowed_files(tmp_path: Path) -> None:
    project, _ = _setup(tmp_path)
    contract = dict(TWO_TESTS, acceptance_criteria=["The change must update `docs/design/retry.md` with the new cap."])
    report = check_contract_feasibility(project, parse_contract(contract))
    assert [c.requirement.origin for c in report.conflicts] == ["contract"]


def test_a_contract_that_includes_the_required_path_is_feasible(tmp_path: Path) -> None:
    project, _ = _setup(tmp_path, TWO_TESTS)
    _policy(project)
    widened = dict(TWO_TESTS, allowed_files=[*TWO_TESTS["allowed_files"], "docs/design/retry.md"])
    assert check_contract_feasibility(project, parse_contract(widened)).feasible


def test_a_requirement_whose_only_allowed_path_is_forbidden_is_a_conflict(tmp_path: Path) -> None:
    project, _ = _setup(tmp_path, TWO_TESTS)
    _policy(project)
    contract = dict(TWO_TESTS, allowed_files=["tests/**", "docs/**"], forbidden_files=["docs/design/**"])
    report = check_contract_feasibility(project, parse_contract(contract))
    assert not report.feasible and "forbidden, excluded or protected" in report.conflicts[0].reason


def test_applies_to_limits_a_requirement_to_contracts_that_touch_that_area(tmp_path: Path) -> None:
    project, _ = _setup(tmp_path, TWO_TESTS)
    _policy(project, applies_to=["src/**"])
    assert check_contract_feasibility(project, parse_contract(TWO_TESTS)).feasible
    src_contract = dict(TWO_TESTS, allowed_files=["src/retry.py"])
    assert not check_contract_feasibility(project, parse_contract(src_contract)).feasible


def test_an_owner_recorded_waiver_is_honoured(tmp_path: Path) -> None:
    project, _ = _setup(tmp_path, TWO_TESTS)
    _policy(project, waived=True)
    assert check_contract_feasibility(project, parse_contract(TWO_TESTS)).feasible


def test_this_repositorys_own_policy_documents_produce_no_false_conflicts() -> None:
    root = Path(__file__).resolve().parent.parent
    for allowed in (["tests/test_a.py"], ["src/stagemesh/x.py"], ["docs/x.md"]):
        contract = parse_contract(dict(TWO_TESTS, allowed_files=allowed))
        assert check_contract_feasibility(root, contract).feasible


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [("docs/design/**", "docs/**", True), ("docs/design/", "docs/**", True), ("docs/design/x.md", "tests/**", False), ("src/**", "docs/**", False)],
)
def test_pattern_overlap(left: str, right: str, expected: bool) -> None:
    assert patterns_overlap(left, right) is expected


def test_admission_refuses_before_any_provider_and_names_the_conflict(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, TWO_TESTS)
    _policy(project)

    state = contract_state(store, project, TASK, auto_plan=False)
    assert state.startswith("unplannable:contract_policy_conflict:") and "docs/design/**" in state

    with pytest.raises(RunReadyRefusal) as refusal:
        _ensure_contract(store, project, TASK, False, {"occurred": False}, lambda _: None)
    assert refusal.value.reason == "contract_policy_conflict"
    assert refusal.value.detail["conflicts"][0]["requirement"]["id"] == "engineering-impact"

    with pytest.raises(ContractRejected) as rejected:  # the executor's own binding step refuses too
        bind_task_contract(store, project, TASK, "0" * 40)
    assert rejected.value.reason == "contract_policy_conflict"
    assert store.task_contract(TASK) is None  # nothing was frozen

    checked = check_contract(project, TASK)
    assert checked["ok"] is False and checked["feasibility"]["conflicts"]


def test_a_project_owner_can_dismiss_a_prose_requirement_that_is_not_really_one(tmp_path: Path) -> None:
    project, _ = _setup(tmp_path, TWO_TESTS)
    (project / "AGENTS.md").write_text("Every change must add an Engineering Impact record under `docs/design/`.\n", encoding="utf-8")
    assert not check_contract_feasibility(project, parse_contract(TWO_TESTS)).feasible
    folder = project / ".stagemesh" / "policy"
    folder.mkdir(parents=True)
    (folder / "required-changes.json").write_text(json.dumps({"version": 1, "requirements": [], "ignore": ["AGENTS.md:1"]}), encoding="utf-8")
    assert check_contract_feasibility(project, parse_contract(TWO_TESTS)).feasible
