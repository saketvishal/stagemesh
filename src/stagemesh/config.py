from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .domain import Stage
from .github import detect_github_repository
from .routing import RoutingMode
from .security import SecurityBoundaryError, WorkspaceBoundary

# How long a passing live provider check stays fresh (routing.live_acceptance_ttl_seconds overrides it): 7 days.
DEFAULT_LIVE_ACCEPTANCE_TTL_SECONDS = 7 * 24 * 60 * 60.0


class ConfigValidationError(ValueError):
    pass


# Default registry entries (their commands live in providers.approved_default_adapters). They are not the only providers allowed.
BUILTIN_PROVIDERS = ("codex", "claude", "grok", "agy")
PROVIDER_STAGES = ("IMPLEMENT", "REVIEW")
SELECTION_POLICIES = ("priority", "round_robin", "least_recently_used", "weighted")
_LEGACY_CAPABILITIES = {"code": "IMPLEMENT", "review": "REVIEW"}
PROVIDER_PROFILES = ("balanced",)
BALANCED_PROVIDER_POOLS = {
    "IMPLEMENT": ("codex", "grok", "claude", "agy"),
    "REVIEW": ("claude", "grok", "agy", "codex"),
}


TIE_BREAKERS = ("issue_number", "created_at")


@dataclass(frozen=True)
class TaskSelectionConfig:
    """How `continue` picks among several eligible tasks (labels are matched case-insensitively)."""

    auto_select: bool = True
    priority_labels: tuple[str, ...] = ("priority:p0", "priority:p1", "priority:p2", "priority:p3")  # best first
    preferred_labels: tuple[str, ...] = ("stagemesh:prep", "prep", "governance", "readiness")  # best first
    excluded_labels: tuple[str, ...] = ("stagemesh:blocked", "stagemesh:deferred")
    tie_breaker: str = "issue_number"
    required_label: str | None = None  # operator run selector (--label): only tasks carrying exactly this label are admitted; never read from config files


@dataclass(frozen=True)
class DiagnosisConfig:
    """Diagnosis before remediation: stop on a repeated failure, optionally ask a separate read-only provider first."""

    repeat_threshold: int = 2
    stop_on_repeat: bool = True
    provider: str | None = None  # a provider name; unset means no diagnostic provider pass
    dispatch: str = "every_failure"  # never | on_repeat | every_failure (only used when provider is set)


@dataclass(frozen=True)
class ProviderSpec:
    """Optional per-provider metadata from the object form of a `providers` entry."""

    capabilities: frozenset[str] = frozenset(PROVIDER_STAGES)
    priority: int | None = None
    weight: int | None = None
    max_concurrency: int | None = None  # simultaneous provider runs allowed under parallel execution
    # Optional per-stage commands. The provider's `command` is the default for every stage; these replace it for one stage only.
    # The provider stays ONE logical provider (same name, same independence), it just runs a different command for that stage.
    implement_command: str | None = None
    review_command: str | None = None


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
class BudgetConfig:
    """Effort limits checked at stage boundaries (see efficiency.py). 0 or null disables a limit."""

    max_provider_calls_per_task: int | None = 30
    max_equivalent_failures: int | None = 5
    max_provider_calls_per_window: int | None = None
    max_reported_usage_per_task: int | None = None


@dataclass(frozen=True)
class MergeConfig:
    mode: str = "pr_only"
    required_checks: tuple[str, ...] = ()
    check_app_id: int | None = None


def _merging(data: dict[str, object], independent: bool) -> MergeConfig:
    if set(data) - {"mode", "required_checks", "check_app_id"}:
        raise ConfigValidationError("merging has unsupported keys")
    mode = data.get("mode", "pr_only")
    if mode not in ("pr_only", "hosted", "stagemesh"):
        raise ConfigValidationError("merging.mode must be pr_only, hosted or stagemesh")
    checks = data.get("required_checks", [])
    if not isinstance(checks, list) or not all(isinstance(c, str) and c.strip() == c and c for c in checks):
        raise ConfigValidationError("merging.required_checks must be a list of non-empty check names")
    if len(set(checks)) != len(checks):
        raise ConfigValidationError("merging.required_checks must not contain duplicates")
    app = data.get("check_app_id")
    if app is not None and (type(app) is not int or app <= 0):
        raise ConfigValidationError("merging.check_app_id must be a positive integer")
    if mode != "pr_only" and (not checks or not independent):
        raise ConfigValidationError("automatic merging requires named checks and independent review")
    if mode == "stagemesh" and app is None:
        raise ConfigValidationError("StageMesh-controlled merging requires a trusted check_app_id")
    return MergeConfig(str(mode), tuple(checks), app)


ADVERSE_UNAVAILABLE_BEHAVIORS = ("hold",)  # fail closed: an unavailable adverse reviewer never yields a pass
DEFAULT_BLOCKING_SEVERITIES = ("blocker", "critical", "error", "high")


@dataclass(frozen=True)
class AdverseReviewPolicy:
    """Typed adverse-review policy. Defining it adds no gate; review evidence is validated against its digest."""

    blocking_severities: tuple[str, ...] = DEFAULT_BLOCKING_SEVERITIES
    unavailable_reviewer: str = "hold"

    def digest(self) -> str:
        canonical = json.dumps(
            {"blocking_severities": sorted(self.blocking_severities), "unavailable_reviewer": self.unavailable_reviewer},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def is_blocking(self, severity: object) -> bool:
        return isinstance(severity, str) and severity.strip().casefold() in self.blocking_severities


def _adverse_review_policy(data: dict[str, object]) -> AdverseReviewPolicy:
    unknown = set(data) - {"blocking_severities", "unavailable_reviewer"}
    if unknown:
        raise ConfigValidationError(f"adverse_review has unsupported keys: {', '.join(sorted(unknown))}")
    severities = data.get("blocking_severities", DEFAULT_BLOCKING_SEVERITIES)
    if (not isinstance(severities, (list, tuple)) or not severities
            or not all(isinstance(item, str) and item.strip() for item in severities)):
        raise ConfigValidationError("adverse_review.blocking_severities must be a non-empty list of severity names")
    behavior = data.get("unavailable_reviewer", "hold")
    if behavior not in ADVERSE_UNAVAILABLE_BEHAVIORS:
        raise ConfigValidationError(f"adverse_review.unavailable_reviewer must be one of {', '.join(ADVERSE_UNAVAILABLE_BEHAVIORS)}")
    return AdverseReviewPolicy(tuple(sorted({item.strip().casefold() for item in severities})), behavior)


def _require_approved_spec(data: dict[str, object]) -> bool:
    """Opt-in planning gate; absent means the legacy policy (no pre-implementation approval required)."""
    unknown = set(data) - {"require_approved_spec"}
    if unknown:
        raise ConfigValidationError(f"planning_gate has unsupported keys: {', '.join(sorted(unknown))}")
    value = data.get("require_approved_spec", False)
    if not isinstance(value, bool):
        raise ConfigValidationError("planning_gate.require_approved_spec must be a boolean")
    return value


@dataclass(frozen=True)
class ResearchConfig:
    """Opt-in read-only researcher run before implementation. Absent config means no research stage."""

    provider: str
    command: str


def _research_config(data: dict[str, object], known_providers: set[str]) -> ResearchConfig | None:
    if not data:
        return None
    unknown = set(data) - {"provider", "command"}
    if unknown:
        raise ConfigValidationError(f"research has unsupported keys: {', '.join(sorted(unknown))}")
    provider, command = _string(data.get("provider")), _string(data.get("command"))
    if not provider or not command or not command.strip():
        raise ConfigValidationError("research requires a provider and a read-only command")
    if provider not in known_providers:
        raise ConfigValidationError(f"research.provider references unknown provider: {provider}")
    return ResearchConfig(provider, command)


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
    live_acceptance_ttl_seconds: float = DEFAULT_LIVE_ACCEPTANCE_TTL_SECONDS
    provider_specs: dict[str, ProviderSpec] = field(default_factory=dict)
    provider_selection_policy: str = "round_robin"
    provider_profile: str | None = None
    provider_weights: dict[str, int] = field(default_factory=dict)
    task_selection: TaskSelectionConfig = field(default_factory=TaskSelectionConfig)
    parallel: ParallelConfig = field(default_factory=ParallelConfig)
    runtime: RuntimeConfig | None = None
    diagnosis: DiagnosisConfig = field(default_factory=DiagnosisConfig)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)
    merging: MergeConfig = field(default_factory=MergeConfig)
    authorized_providers: tuple[str, ...] | None = None  # None: the project has not authorized any provider list
    adverse_review: AdverseReviewPolicy = field(default_factory=AdverseReviewPolicy)
    require_approved_spec: bool = False
    research: ResearchConfig | None = None


@dataclass(frozen=True)
class TaskSourceConfig:
    name: str
    kind: str
    path: Path | None = None
    labels: tuple[str, ...] = ()
    excluded_labels: tuple[str, ...] = ()


def load_config(project: Path, config_path: Path | None = None) -> StageMeshConfig:
    project = project.resolve()
    path = config_path or project / ".stagemesh" / "config.json"
    data: dict[str, object] = {}
    source = "defaults"
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
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
    live_ttl = routing_data.get("live_acceptance_ttl_seconds", DEFAULT_LIVE_ACCEPTANCE_TTL_SECONDS)
    if isinstance(live_ttl, bool) or not isinstance(live_ttl, (int, float)) or live_ttl <= 0:
        raise ConfigValidationError("routing.live_acceptance_ttl_seconds must be a positive number")
    provider_profile = _string(routing_data.get("provider_profile"))
    if provider_profile is not None and provider_profile not in PROVIDER_PROFILES:
        raise ConfigValidationError(f"routing.provider_profile must be one of: {', '.join(PROVIDER_PROFILES)}")
    policy_default = "least_recently_used" if provider_profile == "balanced" else "round_robin"
    policy = os.environ.get("STAGEMESH_PROVIDER_SELECTION_POLICY") or routing_data.get("provider_selection_policy", policy_default)
    if policy not in SELECTION_POLICIES:
        raise ConfigValidationError(
            f"routing.provider_selection_policy must be one of: {', '.join(SELECTION_POLICIES)}"
        )
    provider_pools = _provider_pools(_optional_mapping(routing_data, "pools"))
    if provider_profile == "balanced":
        provider_pools = {
            **{stage: tuple(names) for stage, names in BALANCED_PROVIDER_POOLS.items()},
            **provider_pools,
        }
    provider_weights = _provider_weights(_optional_mapping(routing_data, "provider_weights"))
    known = set(BUILTIN_PROVIDERS) | set(provider_commands)
    for stage, names in provider_pools.items():
        unknown = [n for n in names if n not in known]
        if unknown:
            raise ConfigValidationError(f"routing.pools.{stage} references unknown provider: {', '.join(unknown)}")
    unknown_weights = [n for n in provider_weights if n not in known]
    if unknown_weights:
        raise ConfigValidationError(f"routing.provider_weights references unknown provider: {', '.join(unknown_weights)}")
    if _is_legacy_builtin_priority_routing(policy, provider_pools, provider_specs):
        policy = "round_robin"
        provider_specs = {
            name: ProviderSpec(
                capabilities=spec.capabilities,
                weight=spec.weight,
                max_concurrency=spec.max_concurrency,
            )
            for name, spec in provider_specs.items()
        }
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
        live_acceptance_ttl_seconds=float(live_ttl),
        provider_specs=provider_specs,
        provider_selection_policy=str(policy),
        provider_profile=provider_profile,
        provider_weights=provider_weights,
        task_selection=_task_selection(_optional_mapping(data, "task_selection") or _profile_task_selection(project)),
        parallel=_parallel(_optional_mapping(data, "parallel")),
        runtime=runtime,
        diagnosis=_diagnosis(_optional_mapping(data, "diagnosis"), known),
        merging=_merging(_optional_mapping(data, "merging"), require_review),
        budgets=_budgets(_optional_mapping(data, "budgets")),
        authorized_providers=_authorization(_optional_mapping(data, "authorization"), known),
        adverse_review=_adverse_review_policy(_optional_mapping(data, "adverse_review")),
        require_approved_spec=_require_approved_spec(_optional_mapping(data, "planning_gate")),
        research=_research_config(_optional_mapping(data, "research"), known),
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
    """A provider is `name: "command"` (legacy) or `name: {command, capabilities?, priority?, weight?, max_concurrency?}`.

    The object form may also set `review_command` / `implement_command` (or `commands: {"REVIEW": ..., "IMPLEMENT": ...}`) to run a
    different command for that stage, e.g. a read-only reviewer. `command` stays the default; it may be omitted only when every
    stage the provider serves has its own command.
    """
    commands: dict[str, str] = {}
    specs: dict[str, ProviderSpec] = {}
    for key, value in data.items():
        name = _string(key)
        if not name or any(char.isspace() for char in name):
            raise ConfigValidationError("provider names must be non-empty strings without whitespace")
        if isinstance(value, dict):
            unknown = set(value) - {
                "command", "capabilities", "priority", "weight", "max_concurrency", "implement_command", "review_command", "commands"
            }
            if unknown:
                raise ConfigValidationError(f"provider {name} has unsupported keys: {', '.join(sorted(unknown))}")
            command = _string(value.get("command"))
            capabilities = _provider_capabilities(name, value.get("capabilities"))
            stage_commands = _stage_commands(name, value, capabilities)
            if not command and capabilities <= set(stage_commands):  # every served stage has its own command: the default is never used
                command = stage_commands[min(capabilities)]
            specs[name] = ProviderSpec(
                capabilities=capabilities,
                implement_command=stage_commands.get("IMPLEMENT"),
                review_command=stage_commands.get("REVIEW"),
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


def _stage_commands(name: str, value: dict[str, object], capabilities: frozenset[str]) -> dict[str, str]:
    """Per-stage commands from `implement_command` / `review_command` / `commands`; a stage may be set only one way."""
    found: dict[str, str] = {}
    sources = {"IMPLEMENT": "implement_command", "REVIEW": "review_command"}
    for stage, key in sources.items():
        if value.get(key) is not None:
            command = _string(value.get(key))
            if not command or not command.strip():
                raise ConfigValidationError(f"provider {name} {key} must be a non-empty string")
            found[stage] = command
    grouped = value.get("commands")
    if grouped is not None:
        if not isinstance(grouped, dict) or not grouped:
            raise ConfigValidationError(f"provider {name} commands must be an object keyed by stage ({', '.join(PROVIDER_STAGES)})")
        for key, raw in grouped.items():
            stage = _LEGACY_CAPABILITIES.get(str(key).strip().lower(), str(key).strip().upper())
            if stage not in PROVIDER_STAGES:
                raise ConfigValidationError(f"provider {name} commands has unsupported stage {key!r} (use {', '.join(PROVIDER_STAGES)})")
            command = _string(raw)
            if not command or not command.strip():
                raise ConfigValidationError(f"provider {name} commands.{stage} must be a non-empty string")
            if stage in found:
                raise ConfigValidationError(f"provider {name} sets the {stage} command twice ({sources[stage]} and commands.{stage})")
            found[stage] = command
    unserved = set(found) - set(capabilities)
    if unserved:
        raise ConfigValidationError(f"provider {name} has a command for {', '.join(sorted(unserved))}, a stage it does not serve (capabilities)")
    return found


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


def _is_legacy_builtin_priority_routing(
    policy: object,
    provider_pools: dict[str, tuple[str, ...]],
    provider_specs: dict[str, ProviderSpec],
) -> bool:
    """Normalize the old generated Codex-first config to the current pool-rotation default."""
    legacy_priorities = {"codex": 10, "claude": 20, "grok": 30}
    legacy_pools = {
        "IMPLEMENT": ("codex", "claude", "grok"),
        "REVIEW": ("claude", "grok", "codex"),
    }
    return (
        policy == "priority"
        and provider_pools == legacy_pools
        and set(provider_specs) == set(legacy_priorities)
        and all(provider_specs[name].priority == priority for name, priority in legacy_priorities.items())
    )
def _budgets(data: dict[str, object]) -> BudgetConfig:
    defaults = BudgetConfig()
    unknown = set(data) - set(defaults.__dataclass_fields__)
    if unknown:
        raise ConfigValidationError(f"budgets has unsupported keys: {', '.join(sorted(unknown))}")
    values: dict[str, int | None] = {}
    for name in defaults.__dataclass_fields__:
        value = data.get(name, getattr(defaults, name))
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ConfigValidationError(f"budgets.{name} must be a non-negative integer or null")
        if name == "max_equivalent_failures" and isinstance(value, int) and value == 1:
            raise ConfigValidationError("budgets.max_equivalent_failures must be at least 2 (one failure is not a repeat) or 0 to disable")
        values[name] = value or None
    return BudgetConfig(**values)


def _authorization(data: dict[str, object], known_providers: set[str]) -> tuple[str, ...] | None:
    unknown = set(data) - {"unattended_providers"}
    if unknown:
        raise ConfigValidationError(f"authorization has unsupported keys: {', '.join(sorted(unknown))}")
    if "unattended_providers" not in data:
        return None
    names = data["unattended_providers"]
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names):
        raise ConfigValidationError("authorization.unattended_providers must be a non-empty list of provider names")
    cleaned = tuple(n.strip() for n in names)
    if len(set(cleaned)) != len(cleaned):
        raise ConfigValidationError("authorization.unattended_providers must not contain duplicates")
    unlisted = [n for n in cleaned if n not in known_providers]
    if unlisted:
        raise ConfigValidationError(f"authorization.unattended_providers references unknown provider: {', '.join(unlisted)}")
    return cleaned


def _diagnosis(data: dict[str, object], known_providers: set[str]) -> DiagnosisConfig:
    unknown = set(data) - {"repeat_threshold", "stop_on_repeat", "provider", "dispatch"}
    if unknown:
        raise ConfigValidationError(f"diagnosis has unsupported keys: {', '.join(sorted(unknown))}")
    threshold = data.get("repeat_threshold", 2)
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 2:
        raise ConfigValidationError("diagnosis.repeat_threshold must be an integer >= 2")
    stop = data.get("stop_on_repeat", True)
    if not isinstance(stop, bool):
        raise ConfigValidationError("diagnosis.stop_on_repeat must be a boolean")
    provider = data.get("provider")
    if provider is not None and (not isinstance(provider, str) or provider not in known_providers):
        raise ConfigValidationError(f"diagnosis.provider must name a configured provider (got {provider!r})")
    dispatch = data.get("dispatch", "every_failure")
    if dispatch not in ("never", "on_repeat", "every_failure"):
        raise ConfigValidationError("diagnosis.dispatch must be one of: never, on_repeat, every_failure")
    return DiagnosisConfig(threshold, stop, provider, str(dispatch))


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
    if os.sep == "/" and "\\" in raw and not raw.startswith("\\\\"):
        raw = raw.replace("\\", "/")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        root = data_project / root
    allow_unsafe = data.get("allow_unsafe_worktree_root", False)
    if not isinstance(allow_unsafe, bool):
        raise ConfigValidationError("runtime.allow_unsafe_worktree_root must be a boolean")
    resolved = root.resolve()
    _validate_worktree_root(data_project, resolved, allow_unsafe=allow_unsafe)
    return RuntimeConfig(
        worktree_root=_project_scoped_worktree_root(data_project, resolved),
        allow_unsafe_worktree_root=allow_unsafe,
    )


def _project_scoped_worktree_root(project: Path, root: Path) -> Path:
    project = project.resolve()
    root = root.resolve()
    if _is_inside(root, project / ".stagemesh"):
        return root
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", project.name).strip(".-") or "project"
    digest = hashlib.sha1(str(project).encode("utf-8")).hexdigest()[:10]
    return root / f"{name}-{digest}"


def _validate_worktree_root(project: Path, root: Path, *, allow_unsafe: bool = False) -> None:
    project = project.resolve()
    if root == project:
        raise ConfigValidationError("runtime.worktree_root must not be the project checkout")
    if not allow_unsafe and _is_broad_root(root):
        raise ConfigValidationError(
            "runtime.worktree_root is too broad; choose a project-owned directory such as "
            ".stagemesh/worktrees "
            "or set runtime.allow_unsafe_worktree_root=true after explicit operator review"
        )
    if not allow_unsafe and root == Path.home().resolve():
        raise ConfigValidationError(
            "runtime.worktree_root must not be the home directory; choose a project-owned directory "
            "such as .stagemesh/worktrees"
        )
    if not allow_unsafe and _is_shared_root(root):
        raise ConfigValidationError(
            "runtime.worktree_root must not be a broad shared directory; choose a dedicated "
            "project-owned directory such as .stagemesh/worktrees"
        )
    if not allow_unsafe and root == project.parent.resolve():
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
            excluded_labels = _labels(item.get("excluded_labels", item.get("exclude_labels", ())))
            sources.append(TaskSourceConfig(name=name, kind=kind, labels=labels, excluded_labels=excluded_labels))
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
    if value in (None, "", [], ()):  # `()` is the default for an absent key
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
