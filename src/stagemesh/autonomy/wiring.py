"""Opt-in wiring of the supervisor into the existing lifecycle.

A project opts in with `.stagemesh/autonomy.json` and nothing else: no environment variable or global setting can enable it, so a
project that did not opt in is never supervised. Opted-out projects see no behavior change: none of
the hooks below do anything and the coordinator runs with no guard.

    { "enabled": true,
      "trusted_committer_emails": ["stagemesh@stagemesh.invalid", "codex@provider.example"],
      "max_reconstructs": 1,
      "allow_baseline_ci_failures": true,
      "unknown_identity": "FENCE" }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..persistence import Store
from .gitfacts import TRUSTED_COMMITTER_EMAILS
from .merge_policy import IntegrationPolicy
from .recovery_policy import RecoveryPolicy, UnknownIdentityStrategy

SETTINGS_FILE = "autonomy.json"
# A busy integration ref must not strand a supervised task behind a manual `retry-task`: refreshing is cheap relative to a founder
# instruction, so supervised runs allow at least this many automatic refreshes regardless of `parallel.integration_rebase_attempts`.
SUPERVISED_MIN_REFRESH_ATTEMPTS = 5


@dataclass(frozen=True)
class AutonomySettings:
    enabled: bool = False
    trusted_committer_emails: tuple[str, ...] = TRUSTED_COMMITTER_EMAILS
    max_reconstructs: int = 1
    allow_baseline_ci_failures: bool = True
    baseline_requires_detail: bool = True
    unknown_identity: UnknownIdentityStrategy = UnknownIdentityStrategy.FENCE
    code_checkout: str | None = None  # the checkout whose StageMesh code must be running (StageMesh developing another StageMesh checkout)

    def integration_policy(self) -> IntegrationPolicy:
        return IntegrationPolicy(allow_baseline_ci_failures=self.allow_baseline_ci_failures, baseline_requires_detail=self.baseline_requires_detail)

    def recovery_policy(self) -> RecoveryPolicy:
        return RecoveryPolicy(unknown_identity=self.unknown_identity)


def _flag(data: dict, key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):  # "false" is a truthy string: a config meant to fail closed must not guess
        raise ValueError(f"autonomy.json: {key} must be true or false, not {value!r}")  # noqa: TRY004
    return value


def _count(data: dict, key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"autonomy.json: {key} must be a non-negative integer, not {value!r}")
    return value


def load_settings(runtime_dir: Path) -> AutonomySettings:
    """Settings from `<runtime>/autonomy.json`; an invalid file raises rather than silently disabling the supervisor."""
    path = Path(runtime_dir) / SETTINGS_FILE
    if not path.is_file():
        return AutonomySettings(enabled=False)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")  # noqa: TRY004 - callers treat ValueError as "invalid settings"
    unknown = set(data) - {"enabled", "trusted_committer_emails", "max_reconstructs", "allow_baseline_ci_failures", "baseline_requires_detail", "unknown_identity", "code_checkout"}
    if unknown:
        raise ValueError(f"{path} has unsupported keys: {', '.join(sorted(unknown))}")
    emails = tuple(str(item) for item in data.get("trusted_committer_emails", TRUSTED_COMMITTER_EMAILS))
    return AutonomySettings(
        enabled=_flag(data, "enabled", False),
        trusted_committer_emails=tuple(dict.fromkeys(emails)),
        max_reconstructs=_count(data, "max_reconstructs", 1),
        allow_baseline_ci_failures=_flag(data, "allow_baseline_ci_failures", True),
        baseline_requires_detail=_flag(data, "baseline_requires_detail", True),
        unknown_identity=UnknownIdentityStrategy(str(data.get("unknown_identity", "FENCE")).upper()),
        code_checkout=str(data["code_checkout"]) if data.get("code_checkout") else None,
    )


def settings_for_store(store: Store) -> AutonomySettings:
    """The store lives at `<project>/.stagemesh/stagemesh.sqlite3`; the opt-in file sits beside it. An invalid file raises (fail closed)."""
    try:
        return load_settings(Path(store.db_path).resolve().parent)
    except FileNotFoundError:
        return AutonomySettings(enabled=False)


def project_of(store: Store) -> Path:
    return Path(store.db_path).resolve().parent.parent
