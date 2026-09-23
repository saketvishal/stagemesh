"""Bind a project to the existing coordinator runtime.

This does not add a second coordinator: it only resolves the project's control
repo, state directory, concurrency limit and worker pool, then hands them to
the existing settings, database lifecycle and `BuildRunner`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from build_coordinator.project.definition import ProjectDefinition, ProjectError
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.runner.routing import (
    DEFAULT_ROLE_CAPABILITIES,
    DEFAULT_ROLE_PERMISSIONS,
    DEFAULT_ROLE_STAGES,
)

_ROLE_FOR_TEMPLATE = {
    "builder": "BUILDER",
    "reviewer": "REVIEWER",
    "integration": "INTEGRATION",
    "planner": "PLANNER",
}


def apply_project_environment(project: ProjectDefinition) -> None:
    """Point the process-wide coordinator settings at the project.

    The project decides the control repo, state dir and builder concurrency.
    An explicitly configured `BUILD_COORDINATOR_DATABASE_URL` is kept so
    operators can still use Postgres or an isolated test database; otherwise
    the database is the project's own, inside its state directory.
    """
    os.environ["BUILD_COORDINATOR_REPO_ROOT"] = str(project.root)
    os.environ["BUILD_COORDINATOR_DATA_DIR"] = str(project.state_dir)
    os.environ["BUILD_COORDINATOR_MAX_ACTIVE_BUILDERS"] = str(project.concurrency)
    if not os.getenv("BUILD_COORDINATOR_DATABASE_URL"):
        # Pin the project's own database so a machine-wide coordinator config
        # (~/.build-coordinator/config.json) can never redirect its state.
        os.environ["BUILD_COORDINATOR_DATABASE_URL"] = (
            f"sqlite:///{(project.state_dir / 'coordinator.sqlite3').as_posix()}"
        )
    os.environ["BUILD_COORDINATOR_ALLOWED_WORKSPACE_ROOTS"] = os.pathsep.join(
        str(path) for path in workspace_roots(project)
    )


def workspace_roots(project: ProjectDefinition) -> tuple[Path, ...]:
    return (project.worktrees_dir,)


def worker_worktree(project: ProjectDefinition, worker_id: str) -> str:
    return str(project.worktrees_dir / worker_id)


def worker_branch(project: ProjectDefinition, worker_id: str) -> str:
    return f"stagemesh/{project.project_id}/{worker_id}"


def _expand_template(
    project: ProjectDefinition,
    role_key: str,
    worker_id: str,
    template: dict[str, Any],
    *,
    dry_run: bool,
    isolated: bool = True,
) -> WorkerConfig:
    role = _ROLE_FOR_TEMPLATE[role_key]
    adapter = "fake" if dry_run else str(template.get("adapter") or "subprocess")
    return WorkerConfig(
        worker_id=worker_id,
        role=role,
        provider=str(template.get("provider") or "local"),
        adapter=adapter,
        command=tuple(str(part) for part in (template.get("command") or ())),
        worktree_path=worker_worktree(project, worker_id) if isolated else None,
        branch_name=worker_branch(project, worker_id) if isolated else None,
        timeout_seconds=template.get("timeout_seconds"),
        poll_seconds=template.get("poll_seconds"),
        runtime=str(template.get("runtime") or "local"),
        model=template.get("model"),
        capabilities=tuple(template.get("capabilities") or DEFAULT_ROLE_CAPABILITIES.get(role, ())),
        max_concurrency=int(template.get("max_concurrency", 1)),
        stages=tuple(template.get("stages") or DEFAULT_ROLE_STAGES.get(role, (role.lower(),))),
        permissions=tuple(template.get("permissions") or DEFAULT_ROLE_PERMISSIONS.get(role, ())),
        env=dict(template.get("env") or {}),
        preference=int(template.get("preference", 100)),
        cost=dict(template.get("cost") or {}),
    )


def build_runner_config(project: ProjectDefinition, *, dry_run: bool = False) -> RunnerConfig:
    """Worker pool for a project.

    Precedence: operator `BUILD_COORDINATOR_RUNNER_CONFIG`; the project's
    `runner_config` file; the project's `workers` templates, expanded into
    `concurrency` builders with StageMesh-provisioned worktrees. A role with
    no template becomes an `unconfigured` worker so the runner escalates
    EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED instead of pretending to work.
    """
    if os.getenv("BUILD_COORDINATOR_RUNNER_CONFIG"):
        return RunnerConfig.default(dry_run=dry_run)
    roots = tuple(str(path) for path in workspace_roots(project))
    if project.runner_config is not None:
        loaded = RunnerConfig.from_file(project.runner_config, dry_run=dry_run)
        return _with_project_defaults(loaded, project, roots)

    workers: list[WorkerConfig] = []
    templates = project.worker_templates

    def template_for(role_key: str) -> dict[str, Any]:
        return templates.get(role_key) or {"adapter": "unconfigured"}

    for index in range(1, project.concurrency + 1):
        workers.append(
            _expand_template(project, "builder", f"builder-{index}", template_for("builder"), dry_run=dry_run)
        )
    for index in range(1, project.reviewers + 1):
        workers.append(
            _expand_template(project, "reviewer", f"reviewer-{index}", template_for("reviewer"), dry_run=dry_run)
        )
    workers.append(
        _expand_template(project, "integration", "integration-1", template_for("integration"), dry_run=dry_run)
    )
    planner = templates.get("planner") or templates.get("builder")
    if planner is not None:
        workers.append(
            _expand_template(project, "planner", "planner-1", planner, dry_run=dry_run, isolated=False)
        )
    return RunnerConfig(
        workers=tuple(workers),
        poll_seconds=float(os.getenv("STAGEMESH_POLL_SECONDS", "2")),
        auto_push_allowed=os.getenv("BUILD_COORDINATOR_AUTO_PUSH_ALLOWED", "false").lower() == "true",
        allowed_workspace_roots=roots,
        result_dir=str(project.state_dir / "results"),
        main_ref=project.main_ref,
        remote_name=project.remote_name,
        task_branches=True,
    )


def _with_project_defaults(
    config: RunnerConfig, project: ProjectDefinition, roots: tuple[str, ...]
) -> RunnerConfig:
    import dataclasses

    return dataclasses.replace(
        config,
        allowed_workspace_roots=config.allowed_workspace_roots or roots,
        result_dir=config.result_dir or str(project.state_dir / "results"),
        task_branches=True,
        main_ref=config.main_ref if config.main_ref != "main" else project.main_ref,
        remote_name=config.remote_name if config.remote_name != "origin" else project.remote_name,
    )


def require_git_repo(project: ProjectDefinition) -> None:
    if not (project.root / ".git").exists():
        raise ProjectError(f"{project.root} is not a git repository; StageMesh needs one to provision worktrees")
