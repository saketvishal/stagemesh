from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .domain import Stage
from .github import detect_github_repository
from .routing import RoutingMode
from .security import SecurityBoundaryError, WorkspaceBoundary


class ConfigValidationError(ValueError):
    pass


# Default registry entries (their commands live in providers.approved_default_adapters). They are not the only providers allowed.
BUILTIN_PROVIDERS = ("codex", "claude", "grok")
PROVIDER_STAGES = ("IMPLEMENT", "REVIEW")
SELECTION_POLICIES = ("priority", "round_robin", "least_recently_used", "weighted")
_LEGACY_CAPABILITIES = {"code": "IMPLEMENT", "review": "REVIEW"}


TIE_BREAKERS = ("issue_number", "created_at")


@dataclass(frozen=True)
class TaskSelectionConfig:
    """How `continue` picks among several eligible tasks (labels are matched case-insensitively)."""

    auto_select: bool = True
    priority_labels: tuple[str, ...] = ("priority:p0", "priority:p1", "priority:p2", "priority:p3")  # best first
    preferred_labels: tuple[str, ...] = ("stagemesh:prep", "prep", "governance", "readiness")  # best first
    excluded_labels: tuple[str, ...] = ("stagemesh:blocked", "stagemesh:deferred")
    tie_breaker: str = "issue_number"


@dataclass(frozen=True)
class ProviderSpec:
    """Optional per-provider metadata from the object form of a `providers` entry."""

    capabilities: frozenset[str] = frozenset(PROVIDER_STAGES)
    priority: int | None = None
    weight: int | None = None
    max_concurrency: int | None = None  # simultaneous provider runs allowed under parallel execution


@dataclass(frozen=True)
class ParallelConfig:
    """Limits for `continue --parallel N`."""

    provider_max_concurrency: int = 2  # per provider, unless the provider sets its own max_concurrency
    integration_rebase_attempts: int = 2  # automatic rebases onto an advanced integration ref before leaving a typed state


@dataclass(frozen=True)
class RuntimeConfig:
    """Project-owned runtime paths."""

    worktree_root: Path
    allow_unsafe_worktree_root: bool = False


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
    task_sources: tuple[TaskSourceConfig, ...]
    routing_mode: str
    stage_routes: dict[str, str]
    single_agent_provider: str | None
    database_url: str | None
    source: str
    require_independent_review: bool = True
    integration_ref: str | None = None
    provider_pools: dict[str, tuple[str, ...]] = field(default_factory=dict)
    provider_failure_cooldown_seconds: float = 900.0
    provider_specs: dict[str, ProviderSpec] = field(default_factory=dict)
    provider_selection_policy: str = "priority"
    provider_weights: dict[str, int] = field(default_factory=dict)
    task_selection: TaskSelectionConfig = field(default_factory=TaskSelectionConfig)
    parallel: ParallelConfig = field(default_factory=ParallelConfig)
    runtime: RuntimeConfig | None = None


@dataclass(frozen=True)
class TaskSourceConfig:
    name: str
    kind: str
    path: Path | None = None
    labels: tuple[str, ...] = ()


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
    provider_commands, provider_specs = _parse_providers(providers)
    for name in sorted(set(BUILTIN_PROVIDERS) | set(provider_commands)):
        env_value = os.environ.get("STAGEMESH_" + re.sub("[^A-Za-z0-9]", "_", name).upper() + "_CMD")
        if env_value:
            provider_commands[name] = env_value
    database_url = os.environ.get("STAGEMESH_DATABASE_URL") or _string(data.get("database_url"))
    routing_mode = os.environ.get("STAGEMESH_ROUTING_MODE") or _string(routing_data.get("mode")) or RoutingMode.STAGED
    if routing_mode not in {RoutingMode.SINGLE_AGENT, RoutingMode.STAGED}:
        raise ConfigValidationError(f"unsupported routing mode: {routing_mode}")
    require_review = routing_data.get("require_independent_review", True)
    if not isinstance(require_review, bool):
        raise ConfigValidationError("routing.require_independent_review must be a boolean")
    cooldown = routing_data.get("provider_failure_cooldown_seconds", 900.0)
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or cooldown < 0:
        raise ConfigValidationError("routing.provider_failure_cooldown_seconds must be a non-negative number")
    policy = os.environ.get("STAGEMESH_PROVIDER_SELECTION_POLICY") or routing_data.get("provider_selection_policy", "priority")
    if policy not in SELECTION_POLICIES:
        raise ConfigValidationError(
            f"routing.provider_selection_policy must be one of: {', '.join(SELECTION_POLICIES)}"
        )
    provider_pools = _provider_pools(_optional_mapping(routing_data, "pools"))
    provider_weights = _provider_weights(_optional_mapping(routing_data, "provider_weights"))
    known = set(BUILTIN_PROVIDERS) | set(provider_commands)
    for stage, names in provider_pools.items():
        unknown = [n for n in names if n not in known]
        if unknown:
            raise ConfigValidationError(f"routing.pools.{stage} references unknown provider: {', '.join(unknown)}")
    unknown_weights = [n for n in provider_weights if n not in known]
    if unknown_weights:
        raise ConfigValidationError(f"routing.provider_weights references unknown provider: {', '.join(unknown_weights)}")
    integration_ref = _string(data.get("integration_ref"))
    if integration_ref and not integration_ref.startswith("refs/"):
        integration_ref = f"refs/heads/{integration_ref}"
    runtime = _runtime(project, _optional_mapping(data, "runtime"))
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
        require_independent_review=require_review,
        integration_ref=integration_ref,
        provider_pools=provider_pools,
        provider_failure_cooldown_seconds=float(cooldown),
        provider_specs=provider_specs,
        provider_selection_policy=str(policy),
        provider_weights=provider_weights,
        task_selection=_task_selection(_optional_mapping(data, "task_selection") or _profile_task_selection(project)),
        parallel=_parallel(_optional_mapping(data, "parallel")),
        runtime=runtime,
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


def _parse_providers(data: dict[str, object]) -> tuple[dict[str, str], dict[str, ProviderSpec]]:
    """A provider is `name: "command"` (legacy) or `name: {command, capabilities?, priority?, weight?}`."""
    commands: dict[str, str] = {}
    specs: dict[str, ProviderSpec] = {}
    for key, value in data.items():
        name = _string(key)
        if not name or any(char.isspace() for char in name):
            raise ConfigValidationError("provider names must be non-empty strings without whitespace")
        if isinstance(value, dict):
            unknown = set(value) - {"command", "capabilities", "priority", "weight", "max_concurrency"}
            if unknown:
                raise ConfigValidationError(f"provider {name} has unsupported keys: {', '.join(sorted(unknown))}")
            command = _string(value.get("command"))
            specs[name] = ProviderSpec(
                capabilities=_provider_capabilities(name, value.get("capabilities")),
                priority=_provider_int(name, "priority", value.get("priority"), minimum=0),
                weight=_provider_int(name, "weight", value.get("weight"), minimum=1),
                max_concurrency=_provider_int(name, "max_concurrency", value.get("max_concurrency"), minimum=1),
            )
        else:
            command = _string(value)
        if not command:
            raise ConfigValidationError(f"provider command for {name} must be a non-empty string")
        commands[name] = command
    return commands, specs


def _provider_capabilities(name: str, value: object) -> frozenset[str]:
    if value is None:
        return frozenset(PROVIDER_STAGES)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ConfigValidationError(f"provider {name} capabilities must be a non-empty list of stage names")
    stages = {_LEGACY_CAPABILITIES.get(item.strip().lower(), item.strip().upper()) for item in value}
    invalid = stages - set(PROVIDER_STAGES)
    if invalid:
        raise ConfigValidationError(
            f"provider {name} has unsupported capabilities: {', '.join(sorted(invalid))} (use {', '.join(PROVIDER_STAGES)})"
        )
    return frozenset(stages)


def _parallel(data: dict[str, object]) -> ParallelConfig:
    unknown = set(data) - {"provider_max_concurrency", "integration_rebase_attempts"}
    if unknown:
        raise ConfigValidationError(f"parallel has unsupported keys: {', '.join(sorted(unknown))}")
    defaults = ParallelConfig()
    values: dict[str, int] = {}
    for key, minimum in (("provider_max_concurrency", 1), ("integration_rebase_attempts", 0)):
        value = data.get(key, getattr(defaults, key))
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ConfigValidationError(f"parallel.{key} must be an integer >= {minimum}")
        values[key] = value
    return ParallelConfig(**values)


def _runtime(data_project: Path, data: dict[str, object]) -> RuntimeConfig:
    unknown = set(data) - {"worktree_root", "allow_unsafe_worktree_root"}
    if unknown:
        raise ConfigValidationError(f"runtime has unsupported keys: {', '.join(sorted(unknown))}")
    raw = _string(data.get("worktree_root")) or ".stagemesh/worktrees"
    root = Path(raw).expanduser()
    if not root.is_absolute():
        root = data_project / root
    allow_unsafe = data.get("allow_unsafe_worktree_root", False)
    if not isinstance(allow_unsafe, bool):
        raise ConfigValidationError("runtime.allow_unsafe_worktree_root must be a boolean")
    resolved = root.resolve()
    if not allow_unsafe:
        _validate_worktree_root(data_project, resolved)
    return RuntimeConfig(worktree_root=resolved, allow_unsafe_worktree_root=allow_unsafe)


def _validate_worktree_root(project: Path, root: Path) -> None:
    project = project.resolve()
    if root == project:
        raise ConfigValidationError("runtime.worktree_root must not be the project checkout")
    if _is_broad_root(root):
        raise ConfigValidationError(
            "runtime.worktree_root is too broad; choose a project-owned directory such as "
            ".stagemesh/worktrees "
            "or set runtime.allow_unsafe_worktree_root=true after explicit operator review"
        )
    if root == Path.home().resolve():
        raise ConfigValidationError(
            "runtime.worktree_root must not be the home directory; choose a project-owned directory "
            "such as .stagemesh/worktrees"
        )
    if _is_shared_root(root):
        raise ConfigValidationError(
            "runtime.worktree_root must not be a broad shared directory; choose a dedicated "
            "project-owned directory such as .stagemesh/worktrees"
        )
    if root == project.parent.resolve():
        raise ConfigValidationError(
            "runtime.worktree_root must not be the project parent directory; choose a "
            "project-owned directory such as .stagemesh/worktrees"
        )
    if _is_inside(root, project / ".git"):
        raise ConfigValidationError("runtime.worktree_root must not be inside .git")
    if root == project / ".stagemesh":
        raise ConfigValidationError(
            "runtime.worktree_root must be a child of .stagemesh, not .stagemesh itself"
        )
    if _is_inside(root, project) and not _is_inside(root, project / ".stagemesh"):
        raise ConfigValidationError(
            "runtime.worktree_root inside the project must be under .stagemesh so task worktrees "
            "are not treated as product source"
        )


def _is_broad_root(path: Path) -> bool:
    return path.parent == path


def _is_shared_root(path: Path) -> bool:
    home = Path.home().resolve()
    resolved = path.resolve()
    shared = {
        Path.cwd().anchor,
        os.environ.get("SystemDrive", "") + "\\",
        "/tmp",
        "/var/tmp",
        "/usr",
        "/var",
        "/opt",
        "C:\\Users",
        "C:\\Windows",
        "C:\\Program Files",
        "C:\\Program Files (x86)",
        "C:\\ProgramData",
    }
    try:
        shared.add(str(home.parent))
    except RuntimeError:
        pass
    if str(resolved).casefold().rstrip("\\/") in {
        item.casefold().rstrip("\\/")
        for item in shared
        if item
    }:
        return True
    return _is_legacy_global_worktree_root(resolved)


def _is_legacy_global_worktree_root(path: Path) -> bool:
    return path.name.casefold() == ".sm-wt" and path.parent == Path(path.anchor)


def _is_inside(path: Path, parent: Path) -> bool:
    path = path.resolve()
    parent = parent.resolve()
    return path == parent or parent in path.parents


def _provider_int(name: str, field_name: str, value: object, *, minimum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigValidationError(f"provider {name} {field_name} must be an integer >= {minimum}")
    return value


def _profile_task_selection(project: Path) -> dict[str, object]:
    """Task-selection defaults shipped with the project profile; config.json's own task_selection wins."""
    try:
        raw = json.loads((project / ".stagemesh" / "profile.json").read_text(encoding="utf-8"))
        selection = raw.get("task_selection")
        return selection if isinstance(selection, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def _task_selection(data: dict[str, object]) -> TaskSelectionConfig:
    defaults = TaskSelectionConfig()
    unknown = set(data) - {"auto_select", "priority_labels", "preferred_labels", "excluded_labels", "tie_breaker"}
    if unknown:
        raise ConfigValidationError(f"task_selection has unsupported keys: {', '.join(sorted(unknown))}")
    auto = data.get("auto_select", defaults.auto_select)
    if not isinstance(auto, bool):
        raise ConfigValidationError("task_selection.auto_select must be a boolean")
    tie = data.get("tie_breaker", defaults.tie_breaker)
    if tie not in TIE_BREAKERS:
        raise ConfigValidationError(f"task_selection.tie_breaker must be one of: {', '.join(TIE_BREAKERS)}")

    def labels(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = data.get(key)
        if value is None:
            return default
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ConfigValidationError(f"task_selection.{key} must be a list of non-empty label names")
        normalized = tuple(item.strip() for item in value)
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ConfigValidationError(f"task_selection.{key} must not contain duplicates")
        return normalized

    return TaskSelectionConfig(
        auto_select=auto,
        priority_labels=labels("priority_labels", defaults.priority_labels),
        preferred_labels=labels("preferred_labels", defaults.preferred_labels),
        excluded_labels=labels("excluded_labels", defaults.excluded_labels),
        tie_breaker=str(tie),
    )


def _provider_weights(data: dict[str, object]) -> dict[str, int]:
    weights: dict[str, int] = {}
    for name, value in data.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigValidationError(f"routing.provider_weights.{name} must be an integer >= 1")
        weights[name] = value
    return weights


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


def _provider_pools(data: dict[str, object]) -> dict[str, tuple[str, ...]]:
    pools: dict[str, tuple[str, ...]] = {}
    for key, value in data.items():
        if key not in {"IMPLEMENT", "REVIEW"}:
            raise ConfigValidationError(f"routing.pools supports only IMPLEMENT and REVIEW, got: {key}")
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
            raise ConfigValidationError(f"routing.pools.{key} must be a non-empty list of provider names")
        names = tuple(item.strip() for item in value)
        if len(set(names)) != len(names):
            raise ConfigValidationError(f"routing.pools.{key} must not contain duplicate providers")
        pools[key] = names
    return pools


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
        if kind not in {"json", "google-ax", "github"}:
            raise ConfigValidationError(f"unsupported task source type for {name}: {kind}")
        if kind == "github":
            labels = _labels(item.get("labels", item.get("label", ())))
            sources.append(TaskSourceConfig(name=name, kind=kind, labels=labels))
            seen.add(name)
            continue
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


def _labels(value: object) -> tuple[str, ...]:
    if value in (None, "", []):
        return ()
    if isinstance(value, str):
        labels = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        labels = value
    else:
        raise ConfigValidationError("github task source labels must be a string or list of strings")
    normalized = tuple(label.strip() for label in labels if label.strip())
    if len(set(normalized)) != len(normalized):
        raise ConfigValidationError("github task source labels must not contain duplicates")
    return normalized
