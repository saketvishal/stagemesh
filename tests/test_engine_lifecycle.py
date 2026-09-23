"""Engine guarantees exercised end to end through `stagemesh continue`:
deterministic validation, TWO_REVIEWERS, cleanup, upstream delivery, provider isolation."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from test_project_backlog import WORKER, git, make_project_repo, stagemesh  # noqa: E402

from build_coordinator.project.definition import register_project


def statuses(payload):
    return [(e["task_id"], e["role"], e["status"], e["worker_id"]) for e in payload["final"]["executions"]]


def states(payload):
    return {t["task_id"]: t["state"] for t in payload["final"]["tasks"]}


def events(root: Path, task_id: str, event_type: str | None = None):
    import sqlite3

    with sqlite3.connect(root / ".build-coordinator" / "coordinator.sqlite3") as db:
        query = "select event_type, from_state, to_state, event_data from build_task_events where task_id=?"
        args = [task_id]
        if event_type:
            query += " and event_type=?"
            args.append(event_type)
        rows = db.execute(query + " order by rowid", args).fetchall()
    return [(t, f, to, json.loads(d)) for t, f, to, d in rows]


def run(root, tmp_path, registry, **env):
    register_project(root)
    return stagemesh(["continue", "fixture"], cwd=tmp_path, registry=registry, extra_env=env)


PASS_IF_FIXED = f'"{sys.executable}" -c "import pathlib,sys; sys.exit(0 if pathlib.Path(\'fixed.txt\').exists() else 1)"'
ALWAYS_FAIL = f'"{sys.executable}" -c "raise SystemExit(3)"'


def test_validation_commands_are_run_by_stagemesh_and_gate_the_lifecycle(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"V-1": {"validation": [PASS_IF_FIXED]}}, concurrency=1)
    proc = run(root, tmp_path, registry)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert states(payload) == {"V-1": "DONE"}

    validations = [d for _t, _f, _to, d in events(root, "V-1", "runner.validation")]
    assert [v["passed"] for v in validations] == [False, True]  # the agent's "tests: passed" was not believed
    assert validations[0]["results"][0]["exit_code"] == 1
    sequence = [(f, to) for t, f, to, _ in events(root, "V-1", "task.transitioned")]
    assert ("VALIDATING", "REWORK_REQUIRED") in sequence
    assert sequence.index(("VALIDATING", "REWORK_REQUIRED")) < sequence.index(("VALIDATING", "REVIEW_READY"))
    roles = [(role, status) for _t, role, status, _w in statuses(payload)]
    assert ("REMEDIATION", "SUCCEEDED") in roles and roles.count(("REVIEWER", "SUCCEEDED")) == 1  # reviewed only once it passed


def test_task_that_never_validates_is_blocked_not_reviewed(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"V-2": {"validation": [ALWAYS_FAIL]}}, concurrency=1)
    payload = json.loads(run(root, tmp_path, registry).stdout)
    assert states(payload) == {"V-2": "BLOCKED"}
    assert not [s for s in statuses(payload) if s[1] == "REVIEWER"]
    assert any(d["passed"] is False for _t, _f, _to, d in events(root, "V-2", "runner.validation"))


def test_two_reviewers_requires_two_distinct_independent_approvals(tmp_path, registry):
    root, _ = make_project_repo(
        tmp_path,
        {"R-1": {"review": "TWO_REVIEWERS"}},
        concurrency=1,
        extra={"upstream": {"remote": "origin", "push": True}, "execution": {"concurrency": 1, "reviewers": 2}},
    )
    payload = json.loads(run(root, tmp_path, registry).stdout)
    assert states(payload) == {"R-1": "DONE"}
    reviews = [s for s in statuses(payload) if s[1] == "REVIEWER"]
    builders = {s[3] for s in statuses(payload) if s[1] == "BUILDER"}
    assert len(reviews) == 2 and len({r[3] for r in reviews}) == 2 and not ({r[3] for r in reviews} & builders)
    assert len(events(root, "R-1", "runner.review_approval_recorded")) == 1


def test_two_reviewers_task_is_not_synchronized_when_only_one_reviewer_exists(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"R-2": {"review": "TWO_REVIEWERS"}}, concurrency=1)
    register_project(root)
    out = stagemesh(["project", "sync", "fixture"], cwd=tmp_path, registry=registry)
    result = json.loads(out.stdout)
    assert result["counts"] == {"ERROR": 1} and "reviewers >= 2" in result["tasks"][0]["details"]


def test_integrated_task_branches_are_cleaned_up_but_unfinished_work_is_kept(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"K-1": {}, "K-2": {"validation": [ALWAYS_FAIL]}}, concurrency=1)
    payload = json.loads(run(root, tmp_path, registry).stdout)
    assert states(payload) == {"K-1": "DONE", "K-2": "BLOCKED"}
    branches = git(root, "branch", "--list", "stagemesh/K-*")
    assert "K-1" not in branches, "integrated branch must be removed"
    assert "K-2" in branches, "blocked work stays available for investigation"
    assert [t for t, *_ in events(root, "K-1", "runner.workspace_cleaned")]


def test_push_failure_is_visible_durable_and_recovered(tmp_path, registry):
    root, origin = make_project_repo(tmp_path, {"P-1": {}}, concurrency=1)
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho rejected-by-test >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

    payload = json.loads(run(root, tmp_path, registry).stdout)
    assert states(payload) == {"P-1": "BLOCKED"}, "not DONE while undelivered"
    integration = [s for s in statuses(payload) if s[1] == "INTEGRATION"]
    assert integration and integration[-1][2] == "HUMAN_ACTION_REQUIRED"
    assert "P-1" in git(root, "log", "-1", "--format=%s", "main") or "Integrate P-1" in git(root, "log", "-1", "--format=%s", "main")
    assert "Integrate P-1" not in git(origin, "log", "--format=%s", "main")

    hook.unlink()
    recovered = stagemesh(["project", "retry-push", "fixture"], cwd=tmp_path, registry=registry)
    assert json.loads(recovered.stdout)["retried"] == [
        {"task_id": "P-1", "pushed": True, "detail": json.loads(recovered.stdout)["retried"][0]["detail"]}
    ]
    assert "Integrate P-1" in git(origin, "log", "--format=%s", "main")
    status = json.loads(stagemesh(["project", "status", "fixture"], cwd=tmp_path, registry=registry).stdout)
    assert {t["task_id"]: t["state"] for t in status["tasks"]} == {"P-1": "DONE"}


def test_integration_never_advances_a_checked_out_dirty_branch(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"D-1": {}}, concurrency=1)
    (root / "human-notes.txt").write_text("uncommitted human work\n", encoding="utf-8")
    payload = json.loads(run(root, tmp_path, registry).stdout)
    assert states(payload) == {"D-1": "BLOCKED"}
    assert (root / "human-notes.txt").read_text(encoding="utf-8") == "uncommitted human work\n"
    last = [s for s in statuses(payload) if s[1] == "INTEGRATION"][-1]
    assert last[2] == "HUMAN_ACTION_REQUIRED"


def test_failing_provider_is_routed_around_and_does_not_stop_other_work(tmp_path, registry):
    def template(provider, preference):
        return {
            "provider": provider,
            "adapter": "subprocess",
            "command": [sys.executable, "-P", str(WORKER)],
            "preference": preference,
            "timeout_seconds": 120,
            "env": {"SCRIPTED_WORKER_PROVIDER": provider},
        }

    root, _ = make_project_repo(
        tmp_path,
        {"F-1": {"review": "NONE"}, "F-2": {"review": "NONE"}},
        concurrency=2,
        workers={
            "builder": [template("flaky", 1), template("steady", 2)],
            "reviewer": [template("steady", 1)],
        },
    )
    payload = json.loads(run(root, tmp_path, registry, SCRIPTED_WORKER_FAIL_PROVIDERS="flaky").stdout)
    assert states(payload) == {"F-1": "DONE", "F-2": "DONE"}
    failures = [d for t in ("F-1", "F-2") for _e, _f, _to, d in events(root, t, "runner.provider_failure")]
    assert failures and {f["provider"] for f in failures} == {"flaky"} and failures[0]["failure"] == "RATE_LIMITED"
    finished = {s[3] for s in statuses(payload) if s[2] == "SUCCEEDED" and s[1] == "BUILDER"}
    assert finished and all("steady" in w for w in finished)


def test_agent_process_death_is_recovered_by_a_replacement_worker(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"X-1": {"review": "NONE"}}, concurrency=1)
    payload = json.loads(run(root, tmp_path, registry, SCRIPTED_WORKER_CRASH="X-1").stdout)
    assert states(payload) == {"X-1": "DONE"}
    kinds = [s[2] for s in statuses(payload) if s[1] == "BUILDER"]
    assert kinds == ["LOST", "SUCCEEDED"]
    assert [d for *_x, d in events(root, "X-1", "claim.recovered_from_lost_execution")]


def test_time_budget_stops_new_work_but_finishes_in_flight_work_and_restores_running(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {f"B-{i}": {"review": "NONE"} for i in range(1, 6)}, concurrency=1)
    register_project(root)
    proc = stagemesh(
        ["continue", "fixture", "--timeout", "3"], cwd=tmp_path, registry=registry, extra_env={"SCRIPTED_WORKER_DELAY": "2"}
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    done = [t for t in payload["final"]["tasks"] if t["state"] == "DONE"]
    ready = [t for t in payload["final"]["tasks"] if t["state"] == "READY"]
    assert done and ready, "some work finished, some deliberately not started"
    assert not [e for e in payload["final"]["executions"] if e["status"] in ("LAUNCHED", "RUNNING")]
    import sqlite3

    with sqlite3.connect(root / ".build-coordinator" / "coordinator.sqlite3") as db:
        assert db.execute("select mode from build_coordinator_state").fetchone()[0] == "RUNNING"
