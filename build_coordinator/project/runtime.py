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
    template = _resolve_runtime_template(project, template)
    adapter = "fake" if dry_run and template.get("adapter") != "builtin-git" else str(template.get("adapter") or "subprocess")
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


def _resolve_runtime_template(project: ProjectDefinition, template: dict[str, Any]) -> dict[str, Any]:
    """`runtime: <id>` names a known agent runtime; StageMesh supplies the command."""
    runtime = template.get("runtime")
    if not runtime or template.get("command") or runtime == "auto":
        return template
    from build_coordinator.agents.machine import runtime_template
    from build_coordinator.agents.profiles import PROFILES

    if runtime not in PROFILES:
        return template
    merged = runtime_template(
        runtime, project.main_ref, timeout_seconds=int(template.get("timeout_seconds") or 3600), model=template.get("model")
    )
    merged.update({k: v for k, v in template.items() if k not in {"env"}})
    merged["env"] = {**merged["env"], **(template.get("env") or {})}
    return merged


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
    builder_count = _effective_builder_count(project)

    def templates_for(role_key: str) -> list[dict[str, Any]]:
        entries = templates.get(role_key)
        if entries is None and role_key in {"builder", "reviewer"}:
            entries = [{"runtime": "auto"}]  # no explicit workers: use the machine's verified runtimes
        expanded: list[dict[str, Any]] = []
        for entry in entries or []:
            if entry.get("runtime") == "auto":
                expanded.extend(_machine_templates(project))
            else:
                expanded.append(entry)
        return expanded or [{"adapter": "unconfigured"}]

    def pool(role_key: str, prefix: str, count: int) -> None:
        entries = templates_for(role_key)
        for entry in entries:
            label = str(entry.get("name") or entry.get("provider") or "") if len(entries) > 1 else ""
            for index in range(1, count + 1):
                worker_id = f"{prefix}-{label}-{index}" if label else f"{prefix}-{index}"
                workers.append(_expand_template(project, role_key, worker_id, entry, dry_run=dry_run))

    pool("builder", "builder", builder_count)
    pool("reviewer", "reviewer", project.reviewers)
    integration_template = (templates.get("integration") or [{"adapter": "builtin-git", "provider": "stagemesh"}])[0]
    workers.append(
        _expand_template(project, "integration", "integration-1", integration_template, dry_run=dry_run)
    )
    planner = (templates.get("planner") or templates.get("builder") or [None])[0]
    if planner is not None:
        workers.append(
            _expand_template(project, "planner", "planner-1", planner, dry_run=dry_run, isolated=False)
        )
    from build_coordinator.agents.machine import machine_providers

    return RunnerConfig(
        workers=tuple(workers),
        providers=machine_providers(),
        poll_seconds=float(os.getenv("STAGEMESH_POLL_SECONDS", "2")),
        auto_push_allowed=os.getenv("BUILD_COORDINATOR_AUTO_PUSH_ALLOWED", "false").lower() == "true",
        allowed_workspace_roots=roots,
        result_dir=str(project.state_dir / "results"),
        main_ref=project.main_ref,
        remote_name=None,
        upstream_remote=project.upstream_remote,
        push_upstream=project.push_upstream,
        validation_timeout_seconds=project.validation_timeout_seconds,
        setup_commands=project.setup_commands,
        bootstrap_commands=tuple(command.as_dict() for command in project.bootstrap_commands),
        cleanup_branches=True,
        task_branches=True,
        routing_policy=_project_routing_policy(),
        external_ci_enabled=project.external_ci_enabled,
        external_ci_repo=project.external_ci_repo,
        external_ci_max_consecutive_errors=project.external_ci_max_consecutive_errors,
    )


def _effective_builder_count(project: ProjectDefinition) -> int:
    override = os.getenv("STAGEMESH_PROJECT_CAPACITY_OVERRIDE")
    if not override:
        return project.concurrency
    try:
        requested = int(override)
    except ValueError as exc:
        raise ProjectError("STAGEMESH_PROJECT_CAPACITY_OVERRIDE must be a positive integer") from exc
    if requested < 1:
        raise ProjectError("STAGEMESH_PROJECT_CAPACITY_OVERRIDE must be a positive integer")
    return max(1, min(project.concurrency, requested))


def _project_routing_policy():
    from build_coordinator.runner.routing import DEFAULT_FALLBACK_ON, RoutingPolicy

    # A provider that needs (re)authentication must not stall unrelated work:
    # fall back to any other eligible runtime rather than failing closed.
    return RoutingPolicy(fallback_on=(*DEFAULT_FALLBACK_ON, "AUTH_FAILURE"), no_fallback_on=())


def _machine_templates(project: ProjectDefinition) -> list[dict[str, Any]]:
    from build_coordinator.agents.machine import available_runtime_ids, runtime_template

    return [runtime_template(rid, project.main_ref) for rid in available_runtime_ids()]


def _with_project_defaults(
    config: RunnerConfig, project: ProjectDefinition, roots: tuple[str, ...]
) -> RunnerConfig:
    import dataclasses
    from build_coordinator.agents.machine import machine_providers

    return dataclasses.replace(
        config,
        allowed_workspace_roots=config.allowed_workspace_roots or roots,
        providers=config.providers or machine_providers(),
        result_dir=config.result_dir or str(project.state_dir / "results"),
        setup_commands=config.setup_commands or project.setup_commands,
        bootstrap_commands=config.bootstrap_commands or tuple(command.as_dict() for command in project.bootstrap_commands),
        task_branches=True,
        remote_name=None,
        upstream_remote=project.upstream_remote,
        push_upstream=project.push_upstream,
        cleanup_branches=True,
        main_ref=config.main_ref if config.main_ref != "main" else project.main_ref,
    )


def require_git_repo(project: ProjectDefinition) -> None:
    if not (project.root / ".git").exists():
        raise ProjectError(f"{project.root} is not a git repository; StageMesh needs one to provision worktrees")
