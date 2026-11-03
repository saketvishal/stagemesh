"""Opt-in wiring of the supervisor into the existing lifecycle.

A project opts in with the `autonomy` object in `.stagemesh/config.json`, or the legacy `.stagemesh/autonomy.json`: no environment
variable or global setting can enable it, so a project that did not opt in is never supervised. Defining both locations is rejected as
ambiguous. Opted-out projects see no behavior change: none of the hooks below do anything and the coordinator runs with no guard.

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
PROJECT_CONFIG_FILE = "config.json"
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


def _flag(data: dict, key: str, default: bool, source: Path) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):  # "false" is a truthy string: a config meant to fail closed must not guess
        raise ValueError(f"{source}: autonomy.{key} must be true or false, not {value!r}")  # noqa: TRY004
    return value


def _count(data: dict, key: str, default: int, source: Path) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{source}: autonomy.{key} must be a non-negative integer, not {value!r}")
    return value


def _emails(data: dict[str, object], source: Path) -> tuple[str, ...]:
    value = data.get("trusted_committer_emails", list(TRUSTED_COMMITTER_EMAILS))
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() == item and item for item in value
    ):
        raise ValueError(
            f"{source}: autonomy.trusted_committer_emails must be a list of non-empty strings"
        )
    return tuple(dict.fromkeys(value))


def load_settings(runtime_dir: Path) -> AutonomySettings:
    """Load an explicit project opt-in; invalid or ambiguous configuration fails closed."""
    runtime = Path(runtime_dir)
    legacy_path = runtime / SETTINGS_FILE
    config_path = runtime / PROJECT_CONFIG_FILE
    embedded: object | None = None
    if config_path.is_file():
        config = _json_object(config_path)
        embedded = config.get("autonomy")
        if embedded is not None and not isinstance(embedded, dict):
            raise ValueError(f"{config_path}: autonomy must be a JSON object")
    if legacy_path.is_file() and embedded is not None:
        raise ValueError(
            f"autonomy settings are defined in both {config_path} and {legacy_path}; keep exactly one"
        )
    if embedded is not None:
        return _settings_from_data(embedded, config_path)
    if not legacy_path.is_file():
        return AutonomySettings(enabled=False)
    return _settings_from_data(_json_object(legacy_path), legacy_path)


def _json_object(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} must contain valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")  # noqa: TRY004 - callers treat ValueError as "invalid settings"
    return data


def _settings_from_data(data: dict[str, object], source: Path) -> AutonomySettings:
    unknown = set(data) - {"enabled", "trusted_committer_emails", "max_reconstructs", "allow_baseline_ci_failures", "baseline_requires_detail", "unknown_identity", "code_checkout"}
    if unknown:
        raise ValueError(f"{source}: autonomy has unsupported keys: {', '.join(sorted(unknown))}")
    code_checkout = data.get("code_checkout")
    if code_checkout is not None and (
        not isinstance(code_checkout, str) or not code_checkout.strip()
    ):
        raise ValueError(f"{source}: autonomy.code_checkout must be a non-empty path string")
    unknown_identity = data.get("unknown_identity", "FENCE")
    if not isinstance(unknown_identity, str):
        raise ValueError(f"{source}: autonomy.unknown_identity must be a string")  # noqa: TRY004 - configuration errors use one public exception type
    try:
        identity_strategy = UnknownIdentityStrategy(unknown_identity.upper())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in UnknownIdentityStrategy)
        raise ValueError(
            f"{source}: autonomy.unknown_identity must be one of: {allowed}"
        ) from exc
    return AutonomySettings(
        enabled=_flag(data, "enabled", False, source),
        trusted_committer_emails=_emails(data, source),
        max_reconstructs=_count(data, "max_reconstructs", 1, source),
        allow_baseline_ci_failures=_flag(data, "allow_baseline_ci_failures", True, source),
        baseline_requires_detail=_flag(data, "baseline_requires_detail", True, source),
        unknown_identity=identity_strategy,
        code_checkout=code_checkout,
    )


def settings_for_store(store: Store) -> AutonomySettings:
    """The store lives at `<project>/.stagemesh/stagemesh.sqlite3`; the opt-in file sits beside it. An invalid file raises (fail closed)."""
    try:
        return load_settings(Path(store.db_path).resolve().parent)
    except FileNotFoundError:
        return AutonomySettings(enabled=False)


def project_of(store: Store) -> Path:
    return Path(store.db_path).resolve().parent.parent
