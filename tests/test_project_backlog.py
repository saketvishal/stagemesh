"""Project-owned `.stagemesh/` backlog: discovery, deterministic sync, generic entry point."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import select

from build_coordinator.db import DatabaseLifecycle
from build_coordinator.models import BuildTask, BuildTaskEvent
from build_coordinator.project.backlog import BacklogError, load_backlog, sync_backlog, task_priorities
from build_coordinator.project.definition import (
    ProjectError,
    find_project_root,
    load_project,
    parse_continue_phrase,
    register_project,
    resolve_project,
)
from build_coordinator.project.commands import normalize_argv
from build_coordinator.project.state_migration import migrate_state, plan_migration
from build_coordinator.service import transition_task, upsert_task

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER = REPO_ROOT / "tests" / "_scripted_worker.py"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )
    return result.stdout.strip()


def task_yaml(**tasks: dict) -> str:
    rows = []
    for task_id, extra in tasks.items():
        row = {
            "id": task_id,
            "title": f"Task {task_id}",
            "objective": f"Do {task_id}",
            "acceptance_criteria": [f"{task_id} done"],
            **extra,
        }
        rows.append(row)
    return yaml.safe_dump({"tasks": rows}, sort_keys=False)


def write_project(
    root: Path,
    *,
    project_id: str = "fixture",
    name: str = "Fixture",
    concurrency: int = 3,
    tasks: dict | None = None,
    extra: dict | None = None,
    workers: dict | None = None,
) -> Path:
    (root / ".stagemesh" / "tasks").mkdir(parents=True, exist_ok=True)
    worker = {
        "provider": "scripted",
        "adapter": "subprocess",
        "command": [sys.executable, "-P", str(WORKER)],
        "timeout_seconds": 120,
    }
    definition = {
        "schema_version": 1,
        "id": project_id,
        "name": name,
        "aliases": [project_id],
        "execution": {"concurrency": concurrency, "reviewers": 1, "default_review_policy": "INDEPENDENT"},
        "workers": workers or {"builder": worker, "reviewer": worker},
        **(extra or {}),
    }
    (root / ".stagemesh" / "project.yaml").write_text(yaml.safe_dump(definition), encoding="utf-8")
    if tasks is not None:
        (root / ".stagemesh" / "tasks" / "backlog.yaml").write_text(task_yaml(**tasks), encoding="utf-8")
    return root


@pytest.fixture
def session(tmp_path):
    lifecycle = DatabaseLifecycle(f"sqlite:///{(tmp_path / 'sync.sqlite3').as_posix()}", data_dir=tmp_path)
    lifecycle.initialize_schema()
    with lifecycle.session() as db:
        yield db
    lifecycle.dispose()


# ---------------------------------------------------------------- discovery


def test_project_identified_by_stagemesh_project_yaml(tmp_path, registry):
    root = write_project(tmp_path / "repo")
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == root.resolve()
    assert find_project_root(tmp_path) is None
    project = resolve_project(cwd=nested)
    assert project.project_id == "fixture"
    assert project.tasks_dir == root.resolve() / ".stagemesh" / "tasks"


def test_project_resolved_by_name_from_arbitrary_directory(tmp_path, registry):
    root = write_project(tmp_path / "repo", project_id="alpha", name="Alpha Product")
    register_project(root)
    elsewhere = tmp_path / "somewhere" / "else"
    elsewhere.mkdir(parents=True)
    for phrase in ("alpha", "Alpha", "Alpha Product", "ALPHA PRODUCT"):
        assert resolve_project(phrase, cwd=elsewhere).root == root.resolve()
    with pytest.raises(ProjectError, match="no StageMesh project named 'beta'"):
        resolve_project("beta", cwd=elsewhere)
    with pytest.raises(ProjectError, match="no StageMesh project specified"):
        resolve_project(cwd=elsewhere)


def test_ambiguous_name_is_rejected(tmp_path, registry):
    register_project(write_project(tmp_path / "one", project_id="one", extra={"aliases": ["shared"]}))
    register_project(write_project(tmp_path / "two", project_id="two", extra={"aliases": ["shared"]}))
    with pytest.raises(ProjectError, match="ambiguous"):
        resolve_project("shared", cwd=tmp_path)


def test_linked_worktree_resolves_to_main_project_root(tmp_path, registry):
    root = write_project(tmp_path / "repo")
    git(root, "init", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-m", "init")
    linked = tmp_path / "linked"
    git(root, "worktree", "add", "-b", "feature", str(linked))
    assert (linked / ".stagemesh" / "project.yaml").is_file()
    assert find_project_root(linked) == root.resolve()


def test_invalid_project_definition_reports_every_problem(tmp_path):
    root = tmp_path / "bad"
    (root / ".stagemesh").mkdir(parents=True)
    (root / ".stagemesh" / "project.yaml").write_text(
        "id: Bad Id\nexecution: {concurrency: 0}\nworkers: {wizard: {}}\n", encoding="utf-8"
    )
    with pytest.raises(ProjectError) as info:
        load_project(root)
    text = str(info.value)
    assert "`id` must match" in text and "concurrency" in text and "unknown worker role" in text


@pytest.mark.parametrize(
    "words,expected",
    [
        (["Continue", "Caventra", "development."], "Caventra"),
        (["continue", "StageMesh", "Development"], "StageMesh"),
        (["Continue", "Alpha", "Product", "development."], "Alpha Product"),
        (["continue"], ""),
        (["continue", "development"], ""),
        (["status"], None),
    ],
)
def test_continue_phrase(words, expected):
    assert parse_continue_phrase(words) == expected


def test_natural_language_argv_is_normalised():
    assert normalize_argv(["stagemesh", "Continue Caventra development."]) == [
        "stagemesh",
        "continue",
        "Caventra",
        "development.",
    ]
    assert normalize_argv(["stagemesh", "Continue", "Caventra", "development."]) == [
        "stagemesh",
        "continue",
        "Caventra",
        "development.",
    ]
    assert normalize_argv(["stagemesh", "status"]) == ["stagemesh", "status"]


# ---------------------------------------------------------------- backlog load


def test_backlog_loads_sorted_and_validates(tmp_path):
    root = write_project(
        tmp_path / "repo",
        tasks={
            "B-2": {"dependencies": ["A-1"], "priority": 5},
            "A-1": {},
        },
    )
    (root / ".stagemesh" / "tasks" / "extra.yaml").write_text(
        task_yaml(**{"C-3": {"review": "none", "risk": "high"}}), encoding="utf-8"
    )
    definitions = load_backlog(load_project(root))
    assert [d.task_id for d in definitions] == ["A-1", "B-2", "C-3"]
    assert definitions[1].priority == 5 and definitions[2].review_policy == "NONE"
    assert definitions[0].review_policy == "INDEPENDENT"  # project default


def test_backlog_rejects_structural_errors_without_partial_load(tmp_path):
    root = write_project(tmp_path / "repo", tasks={"A": {"dependencies": ["B"]}, "B": {"dependencies": ["A"]}})
    with pytest.raises(BacklogError, match="dependency cycle"):
        load_backlog(load_project(root))
    (root / ".stagemesh" / "tasks" / "backlog.yaml").write_text(
        task_yaml(**{"A": {"bogus": 1}, "B": {"review": "maybe"}}), encoding="utf-8"
    )
    (root / ".stagemesh" / "tasks" / "dupe.yaml").write_text(task_yaml(A={}), encoding="utf-8")
    with pytest.raises(BacklogError) as info:
        load_backlog(load_project(root))
    text = str(info.value)
    assert "unknown field" in text and "`review` must be one of" in text and "duplicate task id" in text


# ---------------------------------------------------------------- sync


def snapshot(session) -> list[tuple]:
    return [
        (t.task_id, t.state, t.title, tuple(t.dependencies), t.updated_at)
        for t in session.scalars(select(BuildTask).order_by(BuildTask.task_id))
    ]


def test_sync_is_idempotent_and_deterministic(tmp_path, session):
    root = write_project(tmp_path / "repo", tasks={"A-1": {"priority": 1}, "B-2": {"dependencies": ["A-1"]}})
    project = load_project(root)
    definitions = load_backlog(project)
    first = sync_backlog(session, project, definitions)
    session.commit()
    assert first.counts() == {"CREATED": 2}
    events_after_first = session.scalars(select(BuildTaskEvent)).all()
    before = snapshot(session)

    second = sync_backlog(session, project, load_backlog(project))
    session.commit()
    assert second.counts() == {"SKIPPED": 2}
    assert snapshot(session) == before
    assert len(session.scalars(select(BuildTaskEvent)).all()) == len(events_after_first)
    assert [r.task_id for r in first.results] == ["A-1", "B-2"]
    assert task_priorities(session, ["A-1", "B-2"]) == {"A-1": 1, "B-2": 100}


def test_sync_dry_run_writes_nothing(tmp_path, session):
    project = load_project(write_project(tmp_path / "repo", tasks={"A-1": {}}))
    report = sync_backlog(session, project, load_backlog(project), dry_run=True)
    assert report.counts() == {"CREATED": 1}
    assert session.get(BuildTask, "A-1") is None


def test_sync_never_disturbs_runtime_state(tmp_path, session):
    root = write_project(tmp_path / "repo", tasks={"A-1": {}, "B-2": {}, "C-3": {}})
    project = load_project(root)
    sync_backlog(session, project, load_backlog(project))
    transition_task(session, "A-1", "CLAIMED", actor="w")
    transition_task(session, "A-1", "IN_PROGRESS", actor="w")
    for state in ("CLAIMED", "IN_PROGRESS", "VALIDATING", "DONE"):
        transition_task(session, "C-3", state, actor="w")
    session.commit()

    (root / ".stagemesh" / "tasks" / "backlog.yaml").write_text(
        task_yaml(
            **{
                "A-1": {"title": "Changed A"},
                "B-2": {"title": "Changed B", "priority": 7},
                "C-3": {"title": "Changed C"},
            }
        ),
        encoding="utf-8",
    )
    report = sync_backlog(session, project, load_backlog(project))
    session.commit()
    actions = {r.task_id: r.action for r in report.results}
    assert actions == {"A-1": "DEFERRED", "B-2": "UPDATED", "C-3": "FINISHED"}
    assert session.get(BuildTask, "A-1").state == "IN_PROGRESS"
    assert session.get(BuildTask, "A-1").title == "Task A-1"
    assert session.get(BuildTask, "B-2").title == "Changed B"
    assert session.get(BuildTask, "B-2").state == "READY"
    assert session.get(BuildTask, "C-3").state == "DONE"
    assert task_priorities(session, ["B-2"]) == {"B-2": 7}


def test_sync_adopts_existing_task_by_stable_id_without_duplicating(tmp_path, session):
    project = load_project(write_project(tmp_path / "repo", tasks={"OLD-1": {}}))
    definition = load_backlog(project)[0]
    upsert_task(session, definition.to_spec())
    transition_task(session, "OLD-1", "CLAIMED", actor="legacy")
    session.commit()

    report = sync_backlog(session, project, [definition])
    session.commit()
    assert report.counts() == {"ADOPTED": 1}
    assert len(session.scalars(select(BuildTask)).all()) == 1
    assert session.get(BuildTask, "OLD-1").state == "CLAIMED"
    assert sync_backlog(session, project, [definition]).counts() == {"SKIPPED": 1}


def test_sync_reports_orphans_and_unresolvable_dependencies(tmp_path, session):
    root = write_project(tmp_path / "repo", tasks={"A-1": {}, "B-2": {}})
    project = load_project(root)
    sync_backlog(session, project, load_backlog(project))
    (root / ".stagemesh" / "tasks" / "backlog.yaml").write_text(
        task_yaml(**{"A-1": {}, "Z-9": {"dependencies": ["NOPE"]}}), encoding="utf-8"
    )
    report = sync_backlog(session, project, load_backlog(project))
    actions = {r.task_id: r.action for r in report.results}
    assert actions == {"A-1": "SKIPPED", "B-2": "ORPHANED", "Z-9": "ERROR"}
    assert session.get(BuildTask, "B-2") is not None
    assert session.get(BuildTask, "Z-9") is None


# ---------------------------------------------------------------- legacy state


LEGACY_TASKS_DDL = """
CREATE TABLE build_tasks (
    task_id VARCHAR(80) NOT NULL, title VARCHAR(240) NOT NULL, description TEXT NOT NULL,
    acceptance_criteria JSON NOT NULL, dependencies JSON NOT NULL, risk_level VARCHAR(16) NOT NULL,
    review_policy VARCHAR(24) NOT NULL, permitted_scope JSON NOT NULL, required_validation JSON NOT NULL,
    implementation_notes TEXT, program_key VARCHAR(120), base_sha VARCHAR(64),
    migration_allowed BOOLEAN NOT NULL, ownership_scope JSON NOT NULL, state VARCHAR(24) NOT NULL,
    branch_name VARCHAR(240), worktree_path TEXT, current_claim_id VARCHAR(36),
    last_heartbeat_at DATETIME, lease_expires_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (task_id),
    CONSTRAINT chk_build_tasks_state CHECK (state IN ('READY','CLAIMED','IN_PROGRESS','VALIDATING','REVIEW_READY','REVIEWING','INTEGRATING','REWORK_REQUIRED','BLOCKED','FAILED','STALE','RESUMABLE','DONE')),
    CONSTRAINT chk_build_tasks_review_policy CHECK (review_policy IN ('NONE','SELF','INDEPENDENT','TWO_REVIEWERS')),
    CONSTRAINT chk_build_tasks_risk_level CHECK (risk_level IN ('LOW','MEDIUM','HIGH','CRITICAL'))
)
"""


def legacy_database(path: Path, *, live_execution: bool = False) -> None:
    connection = sqlite3.connect(str(path))
    connection.execute(LEGACY_TASKS_DDL)
    connection.execute(
        "INSERT INTO build_tasks (task_id,title,description,acceptance_criteria,dependencies,risk_level,"
        "review_policy,permitted_scope,required_validation,migration_allowed,ownership_scope,state) "
        "VALUES ('LEGACY-1','Legacy','d','[]','[]','MEDIUM','INDEPENDENT','[]','[]',0,'{}','REVIEWING')"
    )
    connection.commit()
    connection.close()


def test_legacy_state_is_refused_then_migrated_with_backup_and_history_kept(tmp_path, session):
    path = tmp_path / "legacy.sqlite3"
    legacy_database(path)
    lifecycle = DatabaseLifecycle(f"sqlite:///{path.as_posix()}", data_dir=tmp_path)
    with pytest.raises(Exception, match="no build_coordinator_schema_version"):
        lifecycle.initialize_schema()
    lifecycle.dispose()

    dry = migrate_state(path, apply=False)
    assert dry.needed and not dry.applied and dry.backup is None
    assert not list(tmp_path.glob("*.bak"))

    applied = migrate_state(path, apply=True)
    assert applied.applied and Path(applied.backup).is_file()
    assert applied.preservation and all(r["identical"] for r in applied.preservation)
    assert not plan_migration(path).needed

    lifecycle = DatabaseLifecycle(f"sqlite:///{path.as_posix()}", data_dir=tmp_path)
    lifecycle.initialize_schema()
    with lifecycle.session() as db:
        task = db.get(BuildTask, "LEGACY-1")
        assert task.state == "REVIEWING" and task.reason_created == "MANUAL" and task.parallel_safe is True
    lifecycle.dispose()
    with sqlite3.connect(applied.backup) as backup:
        assert backup.execute("SELECT state FROM build_tasks").fetchone() == ("REVIEWING",)


# ---------------------------------------------------------------- end to end

E2E_ENV_DROP = (
    "BUILD_COORDINATOR_DATA_DIR",
    "BUILD_COORDINATOR_DATABASE_URL",
    "BUILD_COORDINATOR_RUNNER_CONFIG",
    "BUILD_COORDINATOR_REPO_ROOT",
    "BUILD_COORDINATOR_CONFIG",
    "REPO_ROOT",
)


def make_project_repo(tmp_path: Path, tasks: dict, **kwargs) -> tuple[Path, Path]:
    subdir = kwargs.pop("subdir", "repo")
    origin = tmp_path / ("origin.git" if subdir == "repo" else f"{subdir}-origin.git")
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    root = tmp_path / subdir
    root.mkdir()
    git(root, "init", "-b", "main")
    kwargs.setdefault("extra", {"upstream": {"remote": "origin", "push": True}})
    write_project(root, tasks=tasks, **kwargs)
    (root / ".gitignore").write_text(".build-coordinator/\n", encoding="utf-8")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "init")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "origin", "main")
    return root, origin


def stagemesh(args: list[str], *, cwd: Path, registry: Path, extra_env: dict | None = None, timeout: int = 240):
    env = {k: v for k, v in os.environ.items() if k not in E2E_ENV_DROP}
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "STAGEMESH_PROJECT_REGISTRY": str(registry),
            "STAGEMESH_POLL_SECONDS": "0.2",
            "BUILD_COORDINATOR_AUTO_PUSH_ALLOWED": "true",
            "SCRIPTED_WORKER_DELAY": "1.5",
            **(extra_env or {}),
        }
    )
    return subprocess.run(
        [sys.executable, "-P", "-m", "build_coordinator", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_continue_runs_project_backlog_in_parallel_from_any_directory(tmp_path, registry):
    root, origin = make_project_repo(
        tmp_path,
        {
            "T-1": {"priority": 1},
            "T-2": {"priority": 1},
            "T-3": {"priority": 1},
            "T-4": {"priority": 2, "dependencies": ["T-1"]},
            "T-5": {"priority": 3, "dependencies": ["T-4", "T-2"], "review": "NONE"},
        },
    )
    registered = stagemesh(["project", "register", str(root)], cwd=tmp_path, registry=registry)
    assert registered.returncode == 0, registered.stderr
    unrelated = tmp_path / "totally" / "unrelated"
    unrelated.mkdir(parents=True)
    trace = tmp_path / "trace.jsonl"

    run = stagemesh(
        ["Continue Fixture development."],
        cwd=unrelated,
        registry=registry,
        extra_env={"SCRIPTED_WORKER_TRACE": str(trace), "SCRIPTED_WORKER_REWORK": "T-3"},
    )
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)

    assert payload["backlog_sync"]["counts"] == {"CREATED": 5}
    final = {t["task_id"]: t["state"] for t in payload["final"]["tasks"]}
    assert final == {"T-1": "DONE", "T-2": "DONE", "T-3": "DONE", "T-4": "DONE", "T-5": "DONE"}
    assert payload["peak_parallel_builders"] == 3

    executions = payload["final"]["executions"]
    builders = [e for e in executions if e["role"] == "BUILDER"]
    workspaces = {e["worker_id"]: e["worktree_path"] for e in builders}
    assert len(set(workspaces.values())) >= 3
    for path in workspaces.values():
        assert Path(path).is_relative_to(root / ".build-coordinator" / "worktrees")
        assert (Path(path) / ".git").exists()
    reviewers = [e for e in executions if e["role"] == "REVIEWER"]
    assert reviewers and {e["worker_id"] for e in reviewers}.isdisjoint({e["worker_id"] for e in builders})
    assert all(e["routing"] for e in executions)
    assert [e for e in executions if e["role"] == "INTEGRATION"]
    rework = [(e["role"], e["status"]) for e in executions if e["task_id"] == "T-3"]
    assert ("REMEDIATION", "SUCCEEDED") in rework and rework.count(("REVIEWER", "SUCCEEDED")) == 2
    assert not [e for e in executions if e["task_id"] == "T-5" and e["role"] in ("REVIEWER", "INTEGRATION")]

    order = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    finished = {(e["task_id"], e["role"]): e["finished"] for e in order if e["role"] == "BUILDER"}
    started = {(e["task_id"], e["role"]): e["started"] for e in order if e["role"] == "BUILDER"}
    assert started[("T-4", "BUILDER")] >= finished[("T-1", "BUILDER")]
    assert started[("T-5", "BUILDER")] >= max(finished[("T-4", "BUILDER")], finished[("T-2", "BUILDER")])

    main_files = git(root, "ls-tree", "-r", "--name-only", "origin/main") if git(root, "fetch", "origin") is not None else ""
    for task_id in ("T-1", "T-2", "T-3", "T-4"):
        assert f"stagemesh-scripted/{task_id}.txt" in main_files
    integrated = git(root, "log", "--format=%s", "origin/main")
    for task_id, commits in {"T-1": 1, "T-2": 1, "T-3": 2, "T-4": 1}.items():  # T-3: work plus additive remediation
        assert integrated.count(f"scripted work for {task_id}") == commits
    assert "T-5" not in main_files  # review NONE: never integrated

    again = stagemesh(["continue", "fixture"], cwd=unrelated, registry=registry)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["backlog_sync"]["counts"] == {"SKIPPED": 5}


def test_continue_needs_no_github_and_dry_run_is_readonly(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"G-1": {}})
    register_project(root)
    plan = stagemesh(["continue", "fixture", "--dry-run"], cwd=tmp_path, registry=registry)
    assert plan.returncode == 0, plan.stderr
    payload = json.loads(plan.stdout)
    assert payload["dry_run"] and payload["eligible_now"] == ["G-1"]
    assert payload["backlog_sync"]["counts"] == {"CREATED": 1}
    status = stagemesh(["project", "status", "fixture"], cwd=tmp_path, registry=registry)
    assert json.loads(status.stdout)["tasks"] == []


def test_continue_dry_run_with_task_filters_before_claimable_plan(tmp_path, registry):
    root, _ = make_project_repo(
        tmp_path,
        {
            "P0-OTHER": {"priority": 0},
            "TARGET": {"priority": 100},
            "BLOCKED": {"dependencies": ["TARGET"]},
        },
    )
    register_project(root)

    plan = stagemesh(["continue", "fixture", "--dry-run", "--task", "TARGET"], cwd=tmp_path, registry=registry)

    assert plan.returncode == 0, plan.stderr
    payload = json.loads(plan.stdout)
    assert payload["eligible_now"] == ["TARGET"]
    assert payload["would_run_in_parallel"] == ["TARGET"]
    assert payload["target_tasks"] == [
        {"task_id": "TARGET", "exists": True, "state": "READY", "claimable_now": True, "reason": "claimable"}
    ]


def test_continue_with_missing_target_reports_diagnostic_and_claims_nothing(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"OTHER": {}})
    register_project(root)

    run = stagemesh(["continue", "fixture", "--task", "MISSING", "--once"], cwd=tmp_path, registry=registry)

    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert payload["target_tasks"] == [
        {"task_id": "MISSING", "exists": False, "claimable_now": False, "reason": "task_not_found"}
    ]
    assert payload["cycles"][0]["launched"] == []
    final = {t["task_id"]: t["state"] for t in payload["final"]["tasks"]}
    assert final == {"OTHER": "READY"}


def test_continue_with_blocked_target_reports_dependency_and_claims_nothing(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"DEP": {}, "TARGET": {"dependencies": ["DEP"]}, "OTHER": {}})
    register_project(root)

    run = stagemesh(["continue", "fixture", "--task", "TARGET", "--once"], cwd=tmp_path, registry=registry)

    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert payload["target_tasks"] == [
        {
            "task_id": "TARGET",
            "exists": True,
            "state": "READY",
            "claimable_now": False,
            "reason": "blocked_by_dependencies:DEP",
        }
    ]
    assert payload["cycles"][0]["launched"] == []
    final = {t["task_id"]: t["state"] for t in payload["final"]["tasks"]}
    assert final == {"DEP": "READY", "OTHER": "READY", "TARGET": "READY"}


def test_continue_with_task_runs_only_selected_task_through_lifecycle(tmp_path, registry):
    root, origin = make_project_repo(
        tmp_path,
        {
            "P0-OTHER": {"priority": 0},
            "TARGET": {"priority": 100},
        },
        concurrency=2,
    )
    register_project(root)

    run = stagemesh(["continue", "fixture", "--task", "TARGET"], cwd=tmp_path, registry=registry)

    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    final = {t["task_id"]: t["state"] for t in payload["final"]["tasks"]}
    assert final == {"P0-OTHER": "READY", "TARGET": "DONE"}
    touched = {e["task_id"] for e in payload["final"]["executions"]}
    assert touched == {"TARGET"}
    assert [e["role"] for e in payload["final"]["executions"]] == ["BUILDER", "REVIEWER", "INTEGRATION"]
    assert payload["target_tasks"][0]["task_id"] == "TARGET"
    git(root, "fetch", "origin")
    main_files = git(root, "ls-tree", "-r", "--name-only", "origin/main")
    assert "stagemesh-scripted/TARGET.txt" in main_files
    assert "stagemesh-scripted/P0-OTHER.txt" not in main_files


def test_github_adapter_is_optional_and_never_blocks_local_execution(tmp_path, registry):
    root, _ = make_project_repo(
        tmp_path,
        {"G-1": {"review": "NONE"}},
        extra={"task_sources": {"github": {"enabled": True, "repo": "nobody/nothing"}}},
    )
    register_project(root)
    filtered_entries = []
    shim_dir = tmp_path / "bin_without_gh"
    for entry in os.environ["PATH"].split(os.pathsep):
        entry_path = Path(entry)
        has_gh = any((entry_path / name).exists() for name in ("gh", "gh.exe", "gh.cmd"))
        if not has_gh:
            filtered_entries.append(entry)
        elif hasattr(os, "symlink"):
            shim_dir.mkdir(exist_ok=True)
            try:
                for item in entry_path.iterdir():
                    if item.name not in ("gh", "gh.exe", "gh.cmd"):
                        target_file = shim_dir / item.name
                        if not target_file.exists():
                            try:
                                os.symlink(item, target_file)
                            except OSError:
                                pass
                filtered_entries.append(str(shim_dir))
            except OSError:
                pass
    without_gh = os.pathsep.join(filtered_entries)
    run = stagemesh(["continue", "fixture"], cwd=tmp_path, registry=registry, extra_env={"PATH": without_gh})
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert {t["task_id"]: t["state"] for t in payload["final"]["tasks"]} == {"G-1": "DONE"}
    assert payload["backlog_sync"]["counts"] == {"CREATED": 1}


def test_github_adapter_is_off_unless_the_project_enables_it(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"G-1": {"review": "NONE"}})
    register_project(root)
    run = stagemesh(["continue", "fixture"], cwd=tmp_path, registry=registry)
    assert json.loads(run.stdout)["task_source_adapters"] == []


def test_continue_recovers_crashed_execution_and_finishes(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"R-1": {}, "R-2": {}}, concurrency=2)
    register_project(root)
    run = stagemesh(
        ["continue", "fixture"],
        cwd=tmp_path,
        registry=registry,
        extra_env={"SCRIPTED_WORKER_CRASH": "R-1", "SCRIPTED_WORKER_REWORK": "R-2"},
    )
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    final = {t["task_id"]: t["state"] for t in payload["final"]["tasks"]}
    statuses = [(e["task_id"], e["role"], e["status"]) for e in payload["final"]["executions"]]
    assert ("R-1", "BUILDER", "LOST") in statuses  # crash is recovered, not stranded
    assert final["R-1"] == "DONE"
    assert final["R-2"] in {"DONE", "BLOCKED", "REWORK_REQUIRED"}
    assert any(e for e in statuses if e[0] == "R-2" and e[1] == "REVIEWER")


def test_continue_recovers_after_coordinator_crash_without_losing_work(tmp_path, registry):
    root, _ = make_project_repo(
        tmp_path,
        {
            "K-1": {"review": "NONE"},
            "K-2": {"review": "NONE"},
            "K-3": {"priority": 2, "dependencies": ["K-1"], "review": "NONE"},
        },
        concurrency=2,
    )
    register_project(root)
    env = {k: v for k, v in os.environ.items() if k not in E2E_ENV_DROP}
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "STAGEMESH_PROJECT_REGISTRY": str(registry),
            "STAGEMESH_POLL_SECONDS": "0.2",
            "BUILD_COORDINATOR_AUTO_PUSH_ALLOWED": "true",
            "SCRIPTED_WORKER_DELAY": "8",
        }
    )
    coordinator = subprocess.Popen(
        [sys.executable, "-P", "-m", "build_coordinator", "continue", "fixture"],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    live: list[dict] = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and len(live) < 2:
        time.sleep(0.5)
        status = stagemesh(["project", "status", "fixture"], cwd=tmp_path, registry=registry)
        if status.returncode == 0 and status.stdout.strip():
            live = [e for e in json.loads(status.stdout)["executions"] if e["status"] in ("LAUNCHED", "RUNNING")]
    assert len(live) == 2, "two builders should be running in parallel before the crash"
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(coordinator.pid)], capture_output=True)
    else:
        coordinator.kill()
    coordinator.communicate(timeout=30)

    resumed = stagemesh(
        ["continue", "fixture"], cwd=tmp_path, registry=registry, extra_env={"SCRIPTED_WORKER_DELAY": "0.5"}
    )
    assert resumed.returncode == 0, resumed.stderr
    payload = json.loads(resumed.stdout)
    assert {t["task_id"]: t["state"] for t in payload["final"]["tasks"]} == {
        "K-1": "DONE",
        "K-2": "DONE",
        "K-3": "DONE",
    }
    assert sorted(t for c in payload["cycles"] for t in c["recovered"]) == ["K-1", "K-2"]
    lost = [e for e in payload["final"]["executions"] if e["status"] == "LOST"]
    assert {e["task_id"] for e in lost} == {"K-1", "K-2"}


def test_project_state_is_never_redirected_by_a_machine_wide_coordinator_config(tmp_path, registry):
    root, _ = make_project_repo(tmp_path, {"H-1": {}})
    register_project(root)
    home = tmp_path / "home"
    (home / ".build-coordinator").mkdir(parents=True)
    canary = tmp_path / "canary.sqlite3"
    sqlite3.connect(str(canary)).execute("CREATE TABLE canary (id INTEGER)").connection.close()
    (home / ".build-coordinator" / "config.json").write_text(
        json.dumps({"database_url": f"sqlite:///{canary.as_posix()}", "data_dir": str(tmp_path / "elsewhere")}),
        encoding="utf-8",
    )
    before = canary.read_bytes()
    run = stagemesh(
        ["project", "sync", "fixture"],
        cwd=tmp_path,
        registry=registry,
        extra_env={"HOME": str(home), "USERPROFILE": str(home)},
    )
    assert run.returncode == 0, run.stderr
    assert canary.read_bytes() == before
    assert (root / ".build-coordinator" / "coordinator.sqlite3").is_file()


def test_failed_preservation_check_restores_the_original_database(tmp_path, monkeypatch):
    import build_coordinator.project.state_migration as mig

    path = tmp_path / "legacy.sqlite3"
    legacy_database(path)
    original = mig.inventory(path)
    monkeypatch.setattr(mig, "verify_preservation", lambda *_: [{"table": "build_tasks", "identical": False}])
    with pytest.raises(ProjectError, match="restored from"):
        migrate_state(path, apply=True)
    assert mig.inventory(path) == original
    assert plan_migration(path).needed  # still the untouched legacy schema


def test_migration_module_is_self_contained_in_a_fresh_interpreter(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy_database(path)
    script = (
        "from pathlib import Path\n"
        "from build_coordinator.project.state_migration import migrate_state\n"
        f"r = migrate_state(Path({str(path)!r}), apply=True)\n"
        "assert r.applied and all(t['identical'] for t in r.preservation), r.as_dict()\n"
        "import sqlite3\n"
        f"cols = [c[1] for c in sqlite3.connect({str(path)!r}).execute('pragma table_info(build_tasks)')]\n"
        "assert 'objective_id' in cols, cols\n"
    )
    proc = subprocess.run(
        [sys.executable, "-P", "-c", script],
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("limit", [1, 2])
def test_configured_concurrency_is_the_parallelism_limit(tmp_path, registry, limit):
    root, _ = make_project_repo(
        tmp_path, {f"C-{i}": {"review": "NONE"} for i in range(1, 5)}, concurrency=limit
    )
    register_project(root)
    run = stagemesh(["continue", "fixture"], cwd=tmp_path, registry=registry)
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert all(t["state"] == "DONE" for t in payload["final"]["tasks"])
    assert payload["peak_parallel_builders"] == limit
    assert len({e["worker_id"] for e in payload["final"]["executions"]}) == limit


def test_continue_reloads_project_yaml_between_cycles(tmp_path, registry, monkeypatch, capsys):
    root, _ = make_project_repo(
        tmp_path,
        {},
        concurrency=1,
        workers={"builder": {"adapter": "fake"}, "reviewer": {"adapter": "fake"}},
        extra={},
    )
    monkeypatch.setenv("STAGEMESH_PROJECT_REGISTRY", str(registry))
    monkeypatch.setenv("STAGEMESH_POLL_SECONDS", "0")
    monkeypatch.delenv("BUILD_COORDINATOR_DATABASE_URL", raising=False)
    monkeypatch.delenv("BUILD_COORDINATOR_RUNNER_CONFIG", raising=False)

    reload_builder_counts: list[int] = []

    class ReloadProbeRunner:
        def __init__(self, _session_factory, config, **_kwargs):
            self.config = config
            self.cycles = 0

        def reload_config(self, config, *, live_worker_ids=None):
            self.config = config
            reload_builder_counts.append(sum(1 for worker in config.workers if worker.role == "BUILDER"))

        def run_once(self):
            self.cycles += 1
            if self.cycles == 1:
                project = yaml.safe_load((root / ".stagemesh" / "project.yaml").read_text(encoding="utf-8"))
                project["execution"]["concurrency"] = 3
                (root / ".stagemesh" / "project.yaml").write_text(
                    yaml.safe_dump(project),
                    encoding="utf-8",
                )
            return SimpleNamespace(
                launched=[],
                observed=[],
                recovered=[],
                escalations=[],
            )

    import build_coordinator.project.commands as commands

    monkeypatch.setattr(commands, "BuildRunner", ReloadProbeRunner)
    commands.handle_continue(
        SimpleNamespace(
            target=[],
            project_dir=str(root),
            no_sync=True,
            github=False,
            dry_run=False,
            task_id=None,
            max_cycles=2,
            timeout=None,
            once=False,
            all_projects=False,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert reload_builder_counts == [1, 3]
    assert payload["project"]["concurrency"] == 3
