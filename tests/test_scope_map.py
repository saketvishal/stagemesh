"""Auto-planning derives narrow, task-specific contracts from a project's scope map instead of defaulting to `**`."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

import stagemesh.auto_plan as auto_plan_module
from stagemesh.auto_plan import AutoPlanError, create_contract
from stagemesh.concurrency import contract_conflict
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.contracts import parse_contract
from stagemesh.persistence import Store
from stagemesh.queue_run import QueueRunner, write_scope_overlap
from stagemesh.scope_map import ScopeMapError, derive_scope, load_scope_map, parse_scope_map

from test_parallel import Rig, ScriptedExecutor
from test_queue_run import outcomes, queue

REPO = Path(__file__).resolve().parent.parent
GATE = {"name": "project-acceptance-fake", "command": [sys.executable, "-c", "pass"], "timeout_seconds": 60}


@pytest.fixture
def fake_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auto_plan_module, "detect_gates", lambda project: [dict(GATE)])


def stagemesh_areas():
    areas = load_scope_map(REPO)
    assert areas is not None, "StageMesh ships its own stagemesh.scope.json"
    return areas


def scope(labels: tuple[str, ...], title: str, body: str = ""):
    return derive_scope(stagemesh_areas(), labels, title, body)


# --- representative StageMesh objectives generate narrow scopes ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "labels,title,body,area,must_allow,must_not_allow",
    [
        ((), "Document the queue-run workflow in the README", "", "docs", {"README.md", "docs/**"}, {"src/stagemesh/parallel.py"}),
        (("provider",), "Balance load across implementation providers", "", "provider", {"src/stagemesh/provider_pool.py", "src/stagemesh/cli.py"}, {"src/stagemesh/parallel.py", "README.md"}),
        ((), "Fix the scheduler race in ParallelRunner", "", "queue", {"src/stagemesh/parallel.py", "tests/test_queue_run.py"}, {"src/stagemesh/providers.py", "src/stagemesh/diagnosis.py"}),
        ((), "Rebaseline a stale task and improve diagnosis output", "", "recovery", {"src/stagemesh/recovery.py", "src/stagemesh/baseline.py", "src/stagemesh/diagnosis.py"}, {"src/stagemesh/parallel.py"}),
        (("auto-plan",), "Narrow contracts for generated plans", "", "planning", {"src/stagemesh/auto_plan.py", "src/stagemesh/scope_map.py"}, {"src/stagemesh/providers.py"}),
    ],
)
def test_stagemesh_objectives_get_narrow_scopes(labels, title, body, area, must_allow, must_not_allow) -> None:
    decision = scope(labels, title, body)
    assert decision.mode == "narrow" and area in decision.areas, decision.describe()
    assert not {"**", "*", "**/*"} & set(decision.allowed_files)
    assert must_allow <= set(decision.allowed_files) and not must_not_allow & set(decision.allowed_files)


def test_labels_take_precedence_over_keywords() -> None:
    decision = scope(("documentation",), "Fix the scheduler")  # the label decides, the title keyword is not consulted
    assert decision.areas == ("docs",) and decision.source == "label"


def test_unmatched_and_overly_broad_tasks_are_not_narrow() -> None:
    unmatched = scope((), "Improve things generally")
    assert unmatched.mode == "broad" and "no label or keyword" in unmatched.reason and unmatched.allowed_files == ("**",)
    spanning = scope((), "Provider capacity, scheduler queue and diagnosis recovery")
    assert spanning.mode == "broad" and "too many to bound" in spanning.reason
    assert derive_scope(None, (), "x", "").mode == "broad"  # no scope map at all


def test_scope_map_validation() -> None:
    with pytest.raises(ScopeMapError, match="narrower than the whole repository"):
        parse_scope_map({"schema_version": 1, "areas": [{"id": "all", "keywords": ["x"], "allowed_files": ["**"]}]})
    with pytest.raises(ScopeMapError, match="labels or keywords"):
        parse_scope_map({"schema_version": 1, "areas": [{"id": "a", "allowed_files": ["src/**"]}]})
    with pytest.raises(ScopeMapError):
        parse_scope_map({"areas": []})
    assert stagemesh_areas()  # the shipped map is itself valid


# --- the generated contract ------------------------------------------------------------------------------------------------------------


def _project_with_map(tmp_path: Path, labels: list[str], title: str, areas: bool = True, config: dict | None = None):
    rig = Rig(tmp_path, ["T-1"])
    (rig.project / ".stagemesh" / "contracts" / "T-1.json").unlink()
    backlog = json.loads((rig.project / ".stagemesh" / "backlog.json").read_text(encoding="utf-8"))
    backlog["tasks"][0].update(labels=labels, title=title)
    (rig.project / ".stagemesh" / "backlog.json").write_text(json.dumps(backlog), encoding="utf-8")
    if areas:
        (rig.project / "stagemesh.scope.json").write_text((REPO / "stagemesh.scope.json").read_text(encoding="utf-8"), encoding="utf-8")
    if config is not None:
        (rig.project / ".stagemesh" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    import stagemesh.cli as cli_module

    cli_module._sync_all_sources(rig.store, rig.project, load_config(rig.project), None)
    return rig


def test_generated_contract_is_narrow_and_records_why(tmp_path: Path, fake_gates: None) -> None:
    rig = _project_with_map(tmp_path, ["recovery"], "Add rebind support")
    result = create_contract(rig.store, rig.project, "T-1")
    contract = json.loads(result.path.read_text(encoding="utf-8"))
    assert "**" not in contract["allowed_files"] and "src/stagemesh/recovery.py" in contract["allowed_files"]
    assert contract["max_changed_files"] <= 25 and contract["scope"]["mode"] == "narrow" and contract["scope"]["areas"] == ["recovery"]
    assert "area recovery" in contract["validation_escalation_reasons"][0]
    assert result.scope["summary"].startswith("narrow, area recovery via label")
    audit = [json.loads(r["payload"]) for r in rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='contract.auto_generated'")]
    assert audit[0]["scope"]["mode"] == "narrow"
    parse_contract(contract)  # round-trips through the contract parser


def test_unbounded_scope_is_refused_by_default(tmp_path: Path, fake_gates: None) -> None:
    rig = _project_with_map(tmp_path, [], "Improve things generally")
    with pytest.raises(AutoPlanError) as refused:
        create_contract(rig.store, rig.project, "T-1")
    assert refused.value.reason == "no_bounded_scope" and "broad_scope" in refused.value.next_action
    assert not (rig.project / ".stagemesh" / "contracts" / "T-1.json").exists()
    assert auto_plan_module.plannable(rig.store, rig.project, "T-1").startswith("no bounded file scope")


def test_missing_scope_map_is_refused_by_default(tmp_path: Path, fake_gates: None) -> None:
    rig = _project_with_map(tmp_path, ["recovery"], "Add rebind support", areas=False)
    with pytest.raises(AutoPlanError, match="no scope map"):
        create_contract(rig.store, rig.project, "T-1")


def test_broad_scope_requires_an_explicit_policy_and_is_marked(tmp_path: Path, fake_gates: None) -> None:
    rig = _project_with_map(tmp_path, [], "Improve things generally", config={"auto_plan": {"broad_scope": "allow"}})
    result = create_contract(rig.store, rig.project, "T-1")
    contract = json.loads(result.path.read_text(encoding="utf-8"))
    assert contract["allowed_files"] == ["**"] and contract["scope"]["mode"] == "broad"
    assert "BROAD contract (high risk)" in contract["validation_escalation_reasons"][0]
    assert result.scope["summary"].startswith("BROAD (**)")


def test_auto_plan_config_validation(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["T-1"])
    (rig.project / ".stagemesh" / "config.json").write_text(json.dumps({"auto_plan": {"broad_scope": "sometimes"}}), encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="broad_scope"):
        load_config(rig.project)


# --- queue-run: narrow auto-planned tasks really run in parallel ----------------------------------------------------------------------


def test_queue_run_runs_two_narrow_auto_planned_tasks_in_parallel(tmp_path: Path, fake_gates: None) -> None:
    areas = {
        "schema_version": 1,
        "areas": [
            {"id": "alpha", "labels": ["alpha"], "allowed_files": ["out/A.txt"]},
            {"id": "beta", "labels": ["beta"], "allowed_files": ["out/B.txt"]},
        ],
    }
    rig = Rig(tmp_path, ["A", "B"], labels={"A": ["alpha"], "B": ["beta"]})
    for task_id in ("A", "B"):
        (rig.project / ".stagemesh" / "contracts" / f"{task_id}.json").unlink()
    (rig.project / "stagemesh.scope.json").write_text(json.dumps(areas), encoding="utf-8")
    said: list[tuple[str, str]] = []
    runner = queue(rig, ScriptedExecutor(rig.files, barrier=threading.Barrier(2)), emit=lambda t, text: said.append((t, text)))  # opens only if both run at once
    summary = runner.run()
    assert outcomes(summary) == {"A": "DONE", "B": "DONE"} and summary.succeeded
    assert summary.deferred == []  # nothing was serialized because of an overlapping scope
    contracts = {t: parse_contract(json.loads((rig.project / ".stagemesh" / "contracts" / f"{t}.json").read_text(encoding="utf-8"))) for t in ("A", "B")}
    assert contracts["A"].allowed_files == ("out/A.txt",) and contracts["B"].allowed_files == ("out/B.txt",)
    assert write_scope_overlap(contracts["A"], contracts["B"]) is None and contract_conflict(contracts["A"], contracts["B"]) is None
    assert any("auto-planned contract" in text and "scope: narrow" in text for _, text in said)
    assert any("contract scope is narrow" in text and "area alpha" in text for _, text in said)


def test_queue_run_refuses_an_unbounded_task_and_still_runs_the_bounded_one(tmp_path: Path, fake_gates: None) -> None:
    areas = {"schema_version": 1, "areas": [{"id": "alpha", "labels": ["alpha"], "allowed_files": ["out/A.txt"]}]}
    rig = Rig(tmp_path, ["A", "B"], labels={"A": ["alpha"]})
    for task_id in ("A", "B"):
        (rig.project / ".stagemesh" / "contracts" / f"{task_id}.json").unlink()
    (rig.project / "stagemesh.scope.json").write_text(json.dumps(areas), encoding="utf-8")
    summary = queue(rig, ScriptedExecutor(rig.files)).run()
    assert outcomes(summary) == {"A": "DONE", "B": "REFUSED:auto_plan_failed"}
    refusal = next(t for t in summary.tasks if t.task_id == "B").summary.message
    assert "no bounded file scope" in refusal  # the queue log says why planning was refused
    assert not (rig.project / ".stagemesh" / "contracts" / "B.json").exists()
