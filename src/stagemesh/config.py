from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


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
    source: str


def load_config(project: Path, config_path: Path | None = None) -> StageMeshConfig:
    project = project.resolve()
    path = config_path or project / ".stagemesh" / "config.json"
    data: dict[str, object] = {}
    source = "defaults"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        source = str(path)
    github_data = data.get("github", {}) if isinstance(data.get("github", {}), dict) else {}
    providers = data.get("providers", {}) if isinstance(data.get("providers", {}), dict) else {}
    github = GitHubConfig(
        owner=os.environ.get("STAGEMESH_GITHUB_OWNER") or _string(github_data.get("owner")),
        repo=os.environ.get("STAGEMESH_GITHUB_REPO") or _string(github_data.get("repo")),
        token=os.environ.get("STAGEMESH_GITHUB_TOKEN") or _string(github_data.get("token")),
    )
    provider_commands = {str(key): str(value) for key, value in providers.items()}
    for name in ("codex", "claude", "grok"):
        env_value = os.environ.get(f"STAGEMESH_{name.upper()}_CMD")
        if env_value:
            provider_commands[name] = env_value
    return StageMeshConfig(project=project, github=github, provider_commands=provider_commands, source=source)


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
