"""Opt-in wiring of the supervisor into the existing lifecycle.

A project opts in with `.stagemesh/autonomy.json` (or `STAGEMESH_AUTONOMY=1`). Opted-out projects see no behavior change: none of
the hooks below do anything and the coordinator runs with no guard.

    { "enabled": true,
      "trusted_committer_emails": ["stagemesh@stagemesh.invalid", "codex@provider.example"],
      "max_reconstructs": 1,
      "allow_baseline_ci_failures": true,
      "unknown_identity": "FENCE" }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..persistence import Store
from .gitfacts import TRUSTED_COMMITTER_EMAILS
from .merge_policy import IntegrationPolicy
from .recovery_policy import RecoveryPolicy, UnknownIdentityStrategy

SETTINGS_FILE = "autonomy.json"
ENV_FLAG = "STAGEMESH_AUTONOMY"


@dataclass(frozen=True)
class AutonomySettings:
    enabled: bool = False
    trusted_committer_emails: tuple[str, ...] = TRUSTED_COMMITTER_EMAILS
    max_reconstructs: int = 1
    allow_baseline_ci_failures: bool = True
    unknown_identity: UnknownIdentityStrategy = UnknownIdentityStrategy.FENCE

    def integration_policy(self) -> IntegrationPolicy:
        return IntegrationPolicy(allow_baseline_ci_failures=self.allow_baseline_ci_failures)

    def recovery_policy(self) -> RecoveryPolicy:
        return RecoveryPolicy(unknown_identity=self.unknown_identity)


def load_settings(runtime_dir: Path) -> AutonomySettings:
    """Settings from `<runtime>/autonomy.json`; an invalid file raises rather than silently disabling the supervisor."""
    enabled_by_env = os.environ.get(ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}
    path = Path(runtime_dir) / SETTINGS_FILE
    if not path.is_file():
        return AutonomySettings(enabled=enabled_by_env)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")  # noqa: TRY004 - callers treat ValueError as "invalid settings"
    unknown = set(data) - {"enabled", "trusted_committer_emails", "max_reconstructs", "allow_baseline_ci_failures", "unknown_identity"}
    if unknown:
        raise ValueError(f"{path} has unsupported keys: {', '.join(sorted(unknown))}")
    emails = tuple(str(item) for item in data.get("trusted_committer_emails", TRUSTED_COMMITTER_EMAILS))
    return AutonomySettings(
        enabled=bool(data.get("enabled", False)) or enabled_by_env,
        trusted_committer_emails=tuple(dict.fromkeys((*TRUSTED_COMMITTER_EMAILS, *emails))),
        max_reconstructs=int(data.get("max_reconstructs", 1)),
        allow_baseline_ci_failures=bool(data.get("allow_baseline_ci_failures", True)),
        unknown_identity=UnknownIdentityStrategy(str(data.get("unknown_identity", "FENCE")).upper()),
    )


def settings_for_store(store: Store) -> AutonomySettings:
    """The store lives at `<project>/.stagemesh/stagemesh.sqlite3`; the opt-in file sits beside it. An invalid file raises (fail closed)."""
    try:
        return load_settings(Path(store.db_path).resolve().parent)
    except FileNotFoundError:
        return AutonomySettings(enabled=False)


def project_of(store: Store) -> Path:
    return Path(store.db_path).resolve().parent.parent
