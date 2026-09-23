"""Location-independence coverage for the new `objective create --goal`,
`objective pause`, `objective resume`, and `objective approve` commands --
extending the same guarantee `test_operator_location_independence.py`
already proved for `objective status`/`create`/`run` to the objective
lifecycle surface added in this feature.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_LOCATION_ENV_VARS = (
    "BUILD_COORDINATOR_DATA_DIR",
    "BUILD_COORDINATOR_DATABASE_URL",
    "BUILD_COORDINATOR_RUNNER_CONFIG",
)


def _run_cli(args: list[str], *, cwd: Path, config_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for var in _LOCATION_ENV_VARS:
        env.pop(var, None)
    env["BUILD_COORDINATOR_CONFIG"] = str(config_path)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-P", "-m", "build_coordinator.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def coordinator_workspace(tmp_path: Path):
    control_repo_root = tmp_path / "control-repo"
    control_repo_root.mkdir()
    db_path = tmp_path / "coordinator-data" / "coordinator.sqlite3"
    db_path.parent.mkdir(parents=True)
    config_path = tmp_path / "build-coordinator.json"
    config_path.write_text(
        json.dumps(
            {
                "control_repo_root": str(control_repo_root),
                "database_url": f"sqlite:///{db_path.as_posix()}",
                "data_dir": str(db_path.parent),
            }
        ),
        encoding="utf-8",
    )
    unrelated = tmp_path / "totally-unrelated-cwd"
    unrelated.mkdir()
    another = tmp_path / "another-cwd"
    another.mkdir()
    return {"config_path": config_path, "unrelated": unrelated, "another": another}


def _plan_file(tmp_path: Path, task_id: str) -> Path:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps([{"task_id": task_id, "title": "t", "description": "d", "risk_level": "LOW"}]),
        encoding="utf-8",
    )
    return plan_path


def test_objective_create_with_goal_and_plan_file_is_location_independent(coordinator_workspace, tmp_path):
    plan_path = _plan_file(tmp_path, "LOC-OBJ-A")
    result = _run_cli(
        [
            "objective",
            "create",
            "LOC-OBJ",
            "--goal",
            "Evaluate something",
            "--plan-file",
            str(plan_path),
        ],
        cwd=coordinator_workspace["unrelated"],
        config_path=coordinator_workspace["config_path"],
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["objective_id"] == "LOC-OBJ"
    assert payload["state"] == "ACTIVE"
    assert payload["task_count"] == 1


def test_objective_pause_resume_and_approve_are_location_independent(coordinator_workspace, tmp_path):
    plan_path = _plan_file(tmp_path, "LOC-OBJ2-A")
    create_result = _run_cli(
        ["objective", "create", "LOC-OBJ2", "--goal", "g", "--plan-file", str(plan_path)],
        cwd=coordinator_workspace["unrelated"],
        config_path=coordinator_workspace["config_path"],
    )
    assert create_result.returncode == 0, create_result.stderr

    pause_result = _run_cli(
        ["objective", "pause", "LOC-OBJ2"],
        cwd=coordinator_workspace["another"],
        config_path=coordinator_workspace["config_path"],
    )
    assert pause_result.returncode == 0, pause_result.stderr
    assert json.loads(pause_result.stdout)["state"] == "PAUSED"

    resume_result = _run_cli(
        ["objective", "resume", "LOC-OBJ2"],
        cwd=coordinator_workspace["unrelated"],
        config_path=coordinator_workspace["config_path"],
    )
    assert resume_result.returncode == 0, resume_result.stderr
    assert json.loads(resume_result.stdout)["state"] == "ACTIVE"


def test_bare_free_text_objective_does_not_require_a_manual_plan_from_unrelated_cwd(coordinator_workspace):
    result = _run_cli(
        ["objective", "create", "LOC-OBJ-FREE", "--goal", "bare free-text goal, no plan"],
        cwd=coordinator_workspace["unrelated"],
        config_path=coordinator_workspace["config_path"],
    )
    assert result.returncode == 0, result.stderr
    created = json.loads(result.stdout)
    assert created["state"] == "PLANNING"
    assert created["manual_plan_required"] is False
    assert created["task_count"] == 0
    assert created["open_gates"] == []
    assert created["planner_status"] == "PENDING"


def test_objective_approve_resolves_gate_from_a_different_cwd(coordinator_workspace, tmp_path):
    plan_path = tmp_path / "gated-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "tasks": [{"task_id": "LOC-OBJ3-A", "title": "t", "description": "d", "risk_level": "LOW"}],
                "requested_human_gates": ["ARCHITECTURE_DECISION_REQUIRED"],
            }
        ),
        encoding="utf-8",
    )
    gated_result = _run_cli(
        [
            "objective",
            "create",
            "LOC-OBJ3",
            "--goal",
            "goal with an explicit requested gate",
            "--plan-file",
            str(plan_path),
        ],
        cwd=coordinator_workspace["unrelated"],
        config_path=coordinator_workspace["config_path"],
    )
    assert gated_result.returncode == 0, gated_result.stderr
    created = json.loads(gated_result.stdout)
    assert created["state"] == "HUMAN_GATE"
    gate_id = created["open_gates"][0]["gate_id"]

    approve_result = _run_cli(
        ["objective", "approve", gate_id, "--resolved-by", "operator"],
        cwd=coordinator_workspace["another"],
        config_path=coordinator_workspace["config_path"],
    )
    assert approve_result.returncode == 0, approve_result.stderr
    approved = json.loads(approve_result.stdout)
    assert approved["state"] == "ACTIVE"
    assert approved["open_gates"] == []
