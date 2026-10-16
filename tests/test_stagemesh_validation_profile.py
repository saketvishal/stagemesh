from __future__ import annotations

import json
from pathlib import Path

import pytest

from stagemesh.auto_plan import AutoPlanError, create_contract
from stagemesh.contracts import parse_contract
from stagemesh.persistence import Store
from stagemesh.profile import build_contract, parse_profile, resolve_type
from stagemesh.validation_plan import derive_validation_plan, planned_contract

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "docs" / "profiles" / "stagemesh.profile.json"


@pytest.mark.parametrize("area", ["progress", "attribution"])
def test_focused_label_selects_only_its_real_test_targets(area):
    profile = parse_profile(json.loads(PROFILE.read_text(encoding="utf-8")))
    decision = resolve_type(profile, (f"validation:{area}",), "Scoped change", "")
    contract = parse_contract(build_contract(profile, decision, "Scoped change", "T", "test"))
    selected = planned_contract(contract, derive_validation_plan(contract))
    assert decision.type_id == area.replace("-", "_")
    assert selected.gates == contract.gates
    assert len(selected.gates) == 1
    command = selected.gates[0].command
    assert list(command[:4]) == ["python", "-m", "pytest", "-q"]
    assert all((ROOT / target).is_file() for target in command[4:])
    assert "tests/test_run_ready.py" not in command
    assert all(path != "**" for path in contract.allowed_files)


@pytest.mark.parametrize("labels", [(), ("validation:core", "validation:progress"),
                                     ("validation:progress", "validation:attribution")])
def test_unknown_core_and_multi_area_work_keep_broad_gate(labels):
    profile = parse_profile(json.loads(PROFILE.read_text(encoding="utf-8")))
    decision = resolve_type(profile, labels, "Shared work", "")
    contract = parse_contract(build_contract(profile, decision, "Shared work", "T", "test"))
    plan = derive_validation_plan(contract)
    assert decision.type_id == "core"
    assert plan.risk_level == "HIGH"
    assert "stagemesh-acceptance-lifecycle" in plan.planned_checks
    assert planned_contract(contract, plan).gates == contract.gates


def test_auto_planning_uses_task_label_without_changing_existing_contract(tmp_path):
    project = tmp_path / "project"
    runtime = project / ".stagemesh"
    runtime.mkdir(parents=True)
    (runtime / "profile.json").write_bytes(PROFILE.read_bytes())
    store = Store(runtime / "state.sqlite3")
    store.migrate()
    try:
        store.upsert_task("Progress display", source="github", source_id="T")
        store.cache_source("github", "T", {"labels": ["validation:progress"]}, "OPEN")
        result = create_contract(store, project, "T")
        assert result.gates == ("focused-progress",)
        before = result.path.read_bytes()
        with pytest.raises(AutoPlanError, match="refusing to overwrite"):
            create_contract(store, project, "T")
        assert result.path.read_bytes() == before
    finally:
        store.close()
