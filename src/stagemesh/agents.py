"""Agent plugins: one declarative interface for every coding/review agent, built-in or not.

An `AgentPlugin` states what an agent *is* (id, display name, default command, the stages it can serve, whether it reliably produces
structured review JSON, default concurrency, how to check its health, how to classify its failures, how to normalise its review
output, whether it has a read-only review mode, attribution). What an operator *chose* (enabled, roles, priority, weight, limits,
expiry) lives in `agent_config.py`; the two are merged into the provider settings the queue already uses. Codex, Claude and Grok
are registered through exactly the same `AgentRegistry.register` call a local or future plugin would use, so no provider-specific
behaviour needs to live in queue-run or the coordinator.

Plugins are declarative and carry no secrets: credentials stay in the agent's own CLI login or the `STAGEMESH_<ID>_CMD` override.
"""

from __future__ import annotations

import re
import shutil
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

IMPLEMENT = "IMPLEMENT"
REVIEW = "REVIEW"
AGENT_STAGES = (IMPLEMENT, REVIEW)
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class AgentPluginError(ValueError):
    pass


def default_health(command: str) -> tuple[bool, str]:
    """Is the agent's executable callable on PATH? (No network, no billing or quota call.)"""
    parts = shlex.split(command) if command.strip() else []
    if parts and shutil.which(parts[0]):
        return True, "available"
    return False, f"cli_not_installed: '{parts[0] if parts else ''}' is not callable on PATH"


@dataclass(frozen=True)
class AgentPlugin:
    id: str
    display_name: str
    command: str  # default command template; STAGEMESH_<ID>_CMD or provider config overrides it
    stages: frozenset[str] = frozenset(AGENT_STAGES)
    structured_review: bool = True  # reliably emits the review JSON; if False it is never an implicit reviewer
    default_max_concurrency: int | None = None  # None: the project-wide parallel.provider_max_concurrency applies
    supports_implementation: bool = True
    supports_readonly_review: bool = True
    config_schema: dict[str, str] = field(default_factory=dict)  # optional extra settings: key -> "int" | "str" | "bool"
    prompt_format: str = "plain"
    attribution: dict[str, str] = field(default_factory=dict)  # e.g. {"label": "Claude"}; commits stay StageMesh-authored
    health: Callable[[str], tuple[bool, str]] = default_health
    classify_failure: Callable[[int | None, str, str], tuple[bool, str]] | None = None  # (returncode, stdout, stderr) -> (capacity?, reason)
    response_parser: Callable[[str], str] | None = None  # normalise a review answer (e.g. strip a fenced block) before it is judged

    def __post_init__(self) -> None:
        if not _ID.match(self.id):
            raise AgentPluginError(f"agent id must be lowercase letters, digits, '-' or '_': {self.id!r}")
        if not self.display_name.strip() or not self.command.strip():
            raise AgentPluginError(f"agent {self.id} needs a display name and a default command")
        if not self.stages or not set(self.stages) <= set(AGENT_STAGES):
            raise AgentPluginError(f"agent {self.id} stages must be a non-empty subset of {', '.join(AGENT_STAGES)}")
        if IMPLEMENT in self.stages and not self.supports_implementation:
            raise AgentPluginError(f"agent {self.id} cannot serve IMPLEMENT without implementation support")
        if REVIEW in self.stages and not self.supports_readonly_review:
            raise AgentPluginError(f"agent {self.id} cannot serve REVIEW without a read-only review mode")
        if self.default_max_concurrency is not None and self.default_max_concurrency < 1:
            raise AgentPluginError(f"agent {self.id} default_max_concurrency must be at least 1")
        if any(kind not in ("int", "str", "bool") for kind in self.config_schema.values()):
            raise AgentPluginError(f"agent {self.id} config_schema types must be int, str or bool")

    @property
    def env_command_var(self) -> str:
        return "STAGEMESH_" + re.sub("[^A-Za-z0-9]", "_", self.id).upper() + "_CMD"

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "default_command": self.command,
            "stages": sorted(self.stages),
            "structured_review": self.structured_review,
            "default_max_concurrency": self.default_max_concurrency,
            "supports_implementation": self.supports_implementation,
            "supports_readonly_review": self.supports_readonly_review,
            "config_schema": dict(self.config_schema),
            "prompt_format": self.prompt_format,
            "attribution": dict(self.attribution),
        }


class AgentRegistry:
    """Known agent plugins. Built-ins are registered the same way any other plugin would be."""

    def __init__(self) -> None:
        self._plugins: dict[str, AgentPlugin] = {}
        self._builtin: list[str] = []

    def register(self, plugin: AgentPlugin, *, builtin: bool = False) -> AgentPlugin:
        if plugin.id in self._plugins:
            raise AgentPluginError(f"agent plugin already registered: {plugin.id}")
        self._plugins[plugin.id] = plugin
        if builtin:
            self._builtin.append(plugin.id)
        return plugin

    def get(self, agent_id: str) -> AgentPlugin | None:
        return self._plugins.get(agent_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._plugins)

    def builtin_ids(self) -> tuple[str, ...]:
        return tuple(self._builtin)

    def plugins(self) -> list[AgentPlugin]:
        return list(self._plugins.values())

    def is_builtin(self, agent_id: str) -> bool:
        return agent_id in self._builtin


def _builtin_registry() -> AgentRegistry:
    registry = AgentRegistry()
    for plugin in (
        AgentPlugin("codex", "OpenAI Codex", "codex exec", attribution={"label": "Codex"}),
        AgentPlugin("claude", "Anthropic Claude", "claude -p", attribution={"label": "Claude"}),
        AgentPlugin("grok", "xAI Grok", "grok", attribution={"label": "Grok"}),
    ):
        registry.register(plugin, builtin=True)
    return registry


_DEFAULT = _builtin_registry()


def default_registry() -> AgentRegistry:
    """The process-wide registry (built-ins plus anything registered on it)."""
    return _DEFAULT
