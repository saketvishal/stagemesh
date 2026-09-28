from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .domain import Stage
from .github import detect_github_repository
from .routing import RoutingMode
from .security import SecurityBoundaryError, WorkspaceBoundary


class ConfigValidationError(ValueError):
    pass


@dataclass(frozen=True)
class GitHubConfig:
    owner: str | None = None
    repo: str | None = None
    token: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.owner and self.repo and self.token)


@dataclass(frozen=True)
class StageMeshConfig:
    project: Path
    github: GitHubConfig
    provider_commands: dict[str, str]
    task_sources: tuple["TaskSourceConfig", ...]
    routing_mode: str
    stage_routes: dict[str, str]
    single_agent_provider: str | None
    database_url: str | None
    source: str


@dataclass(frozen=True)
class TaskSourceConfig:
    name: str
    kind: str
    path: Path | None = None


def load_config(project: Path, config_path: Path | None = None) -> StageMeshConfig:
    project = project.resolve()
    path = config_path or project / ".stagemesh" / "config.json"
    data: dict[str, object] = {}
    source = "defaults"
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ConfigValidationError("config file must be valid JSON") from exc
        if not isinstance(loaded, dict):
            raise ConfigValidationError("config root must be an object")
        data = loaded
        source = str(path)
    github_data = _optional_mapping(data, "github")
    providers = _optional_mapping(data, "providers")
    task_sources_data = data.get("task_sources", [])
    routing_data = _optional_mapping(data, "routing")
    stage_routes_data = _optional_mapping(routing_data, "stage_routes")
    detected_github = detect_github_repository(project)
    github = GitHubConfig(
        owner=os.environ.get("STAGEMESH_GITHUB_OWNER") or _string(github_data.get("owner")) or (detected_github.owner if detected_github else None),
        repo=os.environ.get("STAGEMESH_GITHUB_REPO") or _string(github_data.get("repo")) or (detected_github.repo if detected_github else None),
        token=os.environ.get("STAGEMESH_GITHUB_TOKEN") or _string(github_data.get("token")),
    )
    provider_commands = _provider_commands(providers)
    for name in ("codex", "claude", "grok"):
        env_value = os.environ.get(f"STAGEMESH_{name.upper()}_CMD")
        if env_value:
            provider_commands[name] = env_value
    database_url = os.environ.get("STAGEMESH_DATABASE_URL") or _string(data.get("database_url"))
    routing_mode = os.environ.get("STAGEMESH_ROUTING_MODE") or _string(routing_data.get("mode")) or RoutingMode.STAGED
    if routing_mode not in {RoutingMode.SINGLE_AGENT, RoutingMode.STAGED}:
        raise ConfigValidationError(f"unsupported routing mode: {routing_mode}")
    return StageMeshConfig(
        project=project,
        github=github,
        provider_commands=provider_commands,
        task_sources=_task_sources(project, task_sources_data),
        routing_mode=routing_mode,
        stage_routes=_stage_routes(stage_routes_data),
        single_agent_provider=os.environ.get("STAGEMESH_SINGLE_AGENT_PROVIDER")
        or _string(routing_data.get("single_agent_provider")),
        database_url=database_url,
        source=source,
    )


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_mapping(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key, {})
    if value == {}:
        return {}
    if not isinstance(value, dict):
        raise ConfigValidationError(f"{key} must be an object")
    return value


def _provider_commands(data: dict[str, object]) -> dict[str, str]:
    commands: dict[str, str] = {}
    for key, value in data.items():
        name = _string(key)
        command = _string(value)
        if not name:
            raise ConfigValidationError("provider names must be non-empty strings")
        if not command:
            raise ConfigValidationError(f"provider command for {name} must be a non-empty string")
        commands[name] = command
    return commands


def _stage_routes(data: dict[str, object]) -> dict[str, str]:
    routes: dict[str, str] = {}
    valid_stages = {str(stage) for stage in Stage}
    for key, value in data.items():
        stage = _string(key)
        provider = _string(value)
        if stage not in valid_stages:
            raise ConfigValidationError(f"unsupported stage route: {key}")
        if not provider:
            raise ConfigValidationError(f"stage route for {stage} must name a provider")
        routes[stage] = provider
    return routes


def _task_sources(project: Path, value: object) -> tuple[TaskSourceConfig, ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list):
        raise ConfigValidationError("task_sources must be a list")
    sources: list[TaskSourceConfig] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ConfigValidationError("task source entries must be objects")
        name = _string(item.get("name"))
        kind = _string(item.get("type")) or _string(item.get("kind"))
        if not name or any(char.isspace() for char in name):
            raise ConfigValidationError("task source name must be a non-empty string")
        if name in seen:
            raise ConfigValidationError(f"duplicate task source name: {name}")
        if kind != "json":
            raise ConfigValidationError(f"unsupported task source type for {name}: {kind}")
        raw_path = _string(item.get("path"))
        if not raw_path:
            raise ConfigValidationError(f"task source {name} path must be a non-empty string")
        path = Path(raw_path)
        if not path.is_absolute():
            path = project / path
        try:
            resolved_path = WorkspaceBoundary(project).require_inside(path.resolve())
        except SecurityBoundaryError as exc:
            raise ConfigValidationError(f"task source {name} path must stay inside the project") from exc
        sources.append(TaskSourceConfig(name=name, kind=kind, path=resolved_path))
        seen.add(name)
    return tuple(sources)
