from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest
from test_run_ready import _project

import stagemesh.cli as cli_module
from stagemesh.config import ConfigValidationError, TaskSelectionConfig, load_config
from stagemesh.persistence import Store
from stagemesh.run_ready import choose_task
from stagemesh.task_selection import SelectionRefusal, select_next_task


def _continue(project: Path, *argv: str) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli_module.main(["--project", str(project), "continue", "--dry-run", "--json", *argv])
    return code, json.loads(out.getvalue())


def _synced(project: Path) -> Store:
    """Sync the project's backlog into its store without running anything."""
    config = load_config(project)
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    cli_module._sync_all_sources(store, project, config, None)
    return store


def test_p0_beats_p1_and_the_reason_is_in_json(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        ["T-3", "T-2", "T-1"],
        labels={"T-3": ["priority:p2"], "T-2": ["priority:p0"], "T-1": ["priority:p1"]},
    )

    code, result = _continue(project)

    assert code == 0 and result["task_id"] == "T-2" and result["stop_reason"] == "DONE"
    selection = result["selection"]
    assert selection["mode"] == "auto" and selection["task_id"] == "T-2"
    assert "decided by priority label (priority:p0 beats priority:p1)" in selection["reason"]
    assert [c["task_id"] for c in selection["candidates"]] == ["T-2", "T-1", "T-3"]


def test_selection_reason_is_logged_in_text_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _project(tmp_path, ["T-1", "T-2"], labels={"T-2": ["priority:p0"]})
    assert cli_module.main(["--project", str(project), "continue", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "task selection (auto): task T-2" in captured.out + captured.err


def test_blocked_tasks_are_never_selected(tmp_path: Path) -> None:
    project = _project(tmp_path, ["B-1", "T-1"], labels={"B-1": ["priority:p0", "stagemesh:blocked"], "T-1": ["priority:p3"]})
    code, result = _continue(project)
    assert code == 0 and result["task_id"] == "T-1"
    assert {"task_id": "B-1", "reason": "excluded label stagemesh:blocked"} in result["selection"]["skipped"]

    only_blocked = _project(tmp_path / "x", ["B-1"], labels={"B-1": ["stagemesh:blocked"]})
    code, result = _continue(only_blocked)
    assert code == 2 and result["stop_reason"] == "REFUSED:no_eligible_task" and "stagemesh:blocked" in result["message"]


def test_equal_priority_is_broken_by_issue_number_then_by_created_time(tmp_path: Path) -> None:
    ids = ["12", "7", "30"]
    code, result = _continue(_project(tmp_path / "n", ids, labels={i: ["priority:p1"] for i in ids}))
    assert code == 0 and result["task_id"] == "7" and "tie-breaker issue_number" in result["selection"]["reason"]

    config = {"task_selection": {"tie_breaker": "created_at"}}
    code, result = _continue(_project(tmp_path / "c", ids, labels={i: ["priority:p1"] for i in ids}, config=config))
    assert code == 0 and result["task_id"] == "12"  # first created wins


def test_truly_tied_candidates_are_refused_with_the_top_candidates(tmp_path: Path) -> None:
    project = _project(tmp_path, ["A", "B", "C"], config={"task_selection": {"tie_breaker": "created_at"}})
    store = _synced(project)
    store.conn.execute("UPDATE tasks SET created_at=1000.0")
    store.conn.commit()

    with pytest.raises(SelectionRefusal) as refusal:
        select_next_task(store, project, load_config(project).task_selection)

    assert refusal.value.reason == "ambiguous_selection" and "tied" in refusal.value.message
    assert len(refusal.value.detail["candidates"]) == 3
    store.close()


def test_auto_select_can_be_disabled(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1", "T-2"], labels={"T-2": ["priority:p0"]}, config={"task_selection": {"auto_select": False}})
    code, result = _continue(project)
    assert code == 2 and result["stop_reason"] == "REFUSED:multiple_eligible_tasks"
    assert result["detail"]["eligible"] == ["T-2", "T-1"] and result["detail"]["candidates"][0]["task_id"] == "T-2"


def test_exactly_one_eligible_task_just_runs(tmp_path: Path) -> None:
    code, result = _continue(_project(tmp_path, ["T-1"]))
    assert code == 0 and result["selection"]["mode"] == "single" and "the only eligible task" in result["selection"]["reason"]


def test_explicit_task_bypasses_the_policy(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1", "T-2"], labels={"T-2": ["priority:p0"]})
    code, result = _continue(project, "--task", "T-1")
    assert code == 0 and result["task_id"] == "T-1" and result["selection"]["mode"] == "explicit"


def test_preferred_labels_then_valid_contracts_break_priority_ties(tmp_path: Path) -> None:
    project = _project(tmp_path / "a", ["T-1", "T-2"], labels={"T-1": ["priority:p1"], "T-2": ["priority:p1", "stagemesh:prep"]})
    _code, result = _continue(project)
    assert result["task_id"] == "T-2" and "preferred label" in result["selection"]["reason"]

    # T-1 has no contract but gates are detectable, so it needs auto-planning; T-2 has a valid contract and wins the tie.
    (tmp_path / "b").mkdir()
    project = _project(tmp_path / "b", ["T-1", "T-2"], contracts=["T-2"], labels={"T-1": ["priority:p1"], "T-2": ["priority:p1"]})
    (project / "package.json").write_text(json.dumps({"scripts": {"test": "exit 0"}}), encoding="utf-8")
    store = _synced(project)
    selection = select_next_task(store, project, TaskSelectionConfig())
    assert selection.task_id == "T-2" and "valid contract beats one needing auto-planning" in selection.reason
    assert {c["task_id"]: c["contract"] for c in selection.candidates} == {"T-1": "needs_auto_plan", "T-2": "valid"}
    store.close()


def test_contractless_tasks_need_auto_plan_and_generatable_gates(tmp_path: Path) -> None:
    # No contract and no detectable gate (bare repo): skipped while another task is viable.
    project = _project(tmp_path / "a", ["T-1", "T-2"], contracts=["T-2"], labels={"T-1": ["priority:p0"]})
    code, result = _continue(project)
    assert code == 0 and result["task_id"] == "T-2"
    assert any(s["task_id"] == "T-1" and "no validation gate" in s["reason"] for s in result["selection"]["skipped"])

    # Auto-planning disabled: a contract-less task is not selected over a contracted one either.
    project = _project(tmp_path / "b", ["T-1", "T-2"], contracts=["T-2"], labels={"T-1": ["priority:p0"]})
    code, result = _continue(project, "--no-auto-plan")
    assert code == 0 and result["task_id"] == "T-2"
    assert any("auto-planning is disabled" in s["reason"] for s in result["selection"]["skipped"])

    # The only task has no contract and auto-plan is off: the specific refusal is preserved.
    code, result = _continue(_project(tmp_path / "c", ["T-1"], contracts=[]), "--no-auto-plan")
    assert code == 2 and result["stop_reason"] == "REFUSED:missing_contract"


def test_stale_failed_tasks_are_skipped_unless_explicitly_retried(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1", "T-2"], labels={"T-1": ["priority:p0"]})
    store = _synced(project)
    store.advance_task("T-1", "IMPLEMENT")
    candidate = store.add_candidate("T-1", "a" * 40, "codex", True)
    store.add_task_remediation("T-1", "VALIDATE", "a" * 40)

    selection = select_next_task(store, project, TaskSelectionConfig())
    assert selection.task_id == "T-2"
    skip = next(s for s in selection.skipped if s["task_id"] == "T-1")
    assert "remediation pending after failed VALIDATE" in skip["reason"] and "--task" not in skip["reason"]
    assert choose_task(store, project, "T-1", TaskSelectionConfig(), True, None).mode == "explicit"

    store.conn.execute("UPDATE task_remediations SET cleared=1")  # what retry-task does
    store.conn.commit()
    assert select_next_task(store, project, TaskSelectionConfig()).task_id == "T-1"  # retried: back in the running
    assert candidate
    store.close()


def test_recent_provider_pool_exhaustion_is_temporarily_skipped(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1", "T-2"], labels={"T-1": ["priority:p0"]})
    store = _synced(project)
    store.add_audit_event(
        "task.implementation_unsuccessful",
        {
            "task_id": "T-1",
            "reason": "all_implementation_providers_exhausted: claude: no_implementation_change; grok: quota_rate_limit",
        },
    )

    selection = select_next_task(store, project, TaskSelectionConfig())

    assert selection.task_id == "T-2"
    skip = next(s for s in selection.skipped if s["task_id"] == "T-1")
    assert "recent provider pool exhaustion" in skip["reason"]
    assert "continuing with other eligible tasks until provider/task cooldown clears" in skip["reason"]
    assert "--task" not in skip["reason"]
    store.close()


def test_provider_pool_exhaustion_skip_survives_short_provider_cooldown_window(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1", "T-2"], labels={"T-1": ["priority:p0"]})
    store = _synced(project)
    store.add_audit_event(
        "task.implementation_unsuccessful",
        {"task_id": "T-1", "reason": "all_implementation_providers_no_progress: claude: no_implementation_change"},
    )
    store.conn.execute("UPDATE audit_events SET created_at=created_at-1200 WHERE event_type='task.implementation_unsuccessful'")
    store.conn.commit()

    selection = select_next_task(store, project, TaskSelectionConfig())

    assert selection.task_id == "T-2"
    assert any(s["task_id"] == "T-1" and "provider pool exhaustion" in s["reason"] for s in selection.skipped)
    store.close()


def test_provider_pool_exhaustion_recovers_one_task_when_everything_else_is_waiting(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1", "T-2"])
    store = _synced(project)
    for task_id in ("T-1", "T-2"):
        store.add_audit_event(
            "task.implementation_unsuccessful",
            {"task_id": task_id, "reason": "all_implementation_providers_no_progress: claude: no_implementation_change"},
        )

    selection = select_next_task(store, project, TaskSelectionConfig())

    assert selection.mode == "auto_recovery"
    assert selection.task_id == "T-1"
    assert "retrying one bounded task automatically" in selection.reason
    assert all("provider pool exhaustion" in item["reason"] for item in selection.skipped)
    store.close()


def test_integration_failure_marks_a_task_stale(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1", "T-2"])
    store = _synced(project)
    store.add_candidate("T-1", "b" * 40, "codex", True)
    store.add_evidence("T-1", "b" * 40, "INTEGRATION", "FAILED", {})
    selection = select_next_task(store, project, TaskSelectionConfig())
    assert selection.task_id == "T-2" and any("latest integration failed" in s["reason"] for s in selection.skipped)
    store.close()


def test_dependencies_are_respected(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1", "T-2"], labels={"T-2": ["priority:p0"]}, dependencies={"T-2": ["T-1"]})
    _code, result = _continue(project, "--max-steps", "1")
    assert result["task_id"] == "T-1"  # the p0 task waits for its dependency
    assert any(s["task_id"] == "T-2" and "waiting for T-1" in s["reason"] for s in result["selection"]["skipped"])


def test_choose_lets_the_operator_pick(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1", "T-2"], labels={"T-2": ["priority:p0"]})
    store = _synced(project)
    seen = []

    def chooser(candidates):
        seen.extend(c.task_id for c in candidates)
        return "T-1"

    selection = select_next_task(store, project, TaskSelectionConfig(), chooser=chooser)
    assert seen == ["T-2", "T-1"] and selection.task_id == "T-1" and selection.mode == "chosen"
    with pytest.raises(SelectionRefusal) as refusal:
        select_next_task(store, project, TaskSelectionConfig(), chooser=lambda c: None)
    assert refusal.value.reason == "no_selection_made"
    store.close()


def test_choose_refuses_without_an_interactive_terminal(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1", "T-2"])
    code, result = _continue(project, "--choose")
    assert code == 2 and result["stop_reason"] == "REFUSED:no_selection_made"


def test_custom_priority_excluded_and_preferred_labels(tmp_path: Path) -> None:
    config = {
        "task_selection": {
            "priority_labels": ["urgent", "normal"],
            "excluded_labels": ["wontfix"],
            "preferred_labels": ["quick-win"],
        }
    }
    project = _project(tmp_path, ["T-1", "T-2", "T-3"], labels={"T-1": ["normal"], "T-2": ["urgent", "wontfix"], "T-3": ["URGENT"]}, config=config)
    code, result = _continue(project)
    assert code == 0 and result["task_id"] == "T-3"  # labels match case-insensitively; excluded T-2 is skipped
    assert result["selection"]["policy"]["priority_labels"] == ["urgent", "normal"]


@pytest.mark.parametrize(
    "bad",
    [
        {"auto_select": "yes"},
        {"tie_breaker": "random"},
        {"priority_labels": "priority:p0"},
        {"priority_labels": ["a", "A"]},
        {"excluded_labels": [""]},
        {"unknown": 1},
    ],
)
def test_task_selection_config_is_validated(tmp_path: Path, bad: dict) -> None:
    (tmp_path / ".stagemesh").mkdir()
    (tmp_path / ".stagemesh" / "config.json").write_text(json.dumps({"task_selection": bad}), encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        load_config(tmp_path)
