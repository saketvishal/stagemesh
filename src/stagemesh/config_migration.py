from __future__ import annotations

import copy
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigValidationError

LEGACY_REVIEW_SUFFIXES = ("-review", "_review")
MIGRATE_COMMAND = "stagemesh config migrate --write"
_KNOWN_STAGE_PROVIDER_BASES = {"claude", "grok"}


@dataclass(frozen=True)
class ConfigMigrationResult:
    config_path: Path
    dry_run: bool
    changed: bool
    backup_path: Path | None
    data: dict[str, Any]
    changes: tuple[str, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "dry_run": self.dry_run,
            "changed": self.changed,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "changes": list(self.changes),
            "limitations": list(self.limitations),
            "data": self.data,
        }


def config_path_for(project: Path, config_path: Path | None = None) -> Path:
    return config_path or project / ".stagemesh" / "config.json"


def legacy_review_config_hint(project: Path, config_path: Path | None = None) -> str | None:
    path = config_path_for(project, config_path)
    try:
        result = plan_provider_stage_command_migration(path)
    except ConfigValidationError:
        return None
    if not result.changed:
        return None
    return f"legacy provider review config detected; run `{MIGRATE_COMMAND}`"


def plan_provider_stage_command_migration(config_path: Path, *, dry_run: bool = True) -> ConfigMigrationResult:
    path = Path(config_path)
    data = _read_config(path)
    migrated, changes, limitations = migrate_provider_stage_commands(data)
    return ConfigMigrationResult(
        config_path=path,
        dry_run=dry_run,
        changed=bool(changes),
        backup_path=None,
        data=migrated,
        changes=tuple(changes),
        limitations=tuple(limitations),
    )


def write_provider_stage_command_migration(config_path: Path) -> ConfigMigrationResult:
    path = Path(config_path)
    planned = plan_provider_stage_command_migration(path, dry_run=False)
    if not planned.changed:
        return planned
    backup = _next_backup_path(path)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    path.write_text(json.dumps(planned.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return ConfigMigrationResult(
        config_path=path,
        dry_run=False,
        changed=True,
        backup_path=backup,
        data=planned.data,
        changes=planned.changes,
        limitations=planned.limitations,
    )


def migrate_provider_stage_commands(data: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    if not isinstance(data, dict):
        raise ConfigValidationError("config root must be an object")
    migrated = copy.deepcopy(data)
    providers = migrated.get("providers")
    if providers is None:
        return migrated, [], []
    if not isinstance(providers, dict):
        raise ConfigValidationError("providers must be an object")

    changes: list[str] = []
    limitations: list[str] = []
    legacy_to_base: dict[str, str] = {}

    for name in list(providers):
        base = _legacy_review_base(name)
        if base is None:
            continue
        if base not in providers:
            limitations.append(f"{name}: no base provider named {base}; left unchanged")
            continue
        review_command = _provider_default_command(providers[name])
        if not review_command:
            limitations.append(f"{name}: no default command to move into {base}.review_command; left unchanged")
            continue
        base_value = providers[base]
        base_spec = _as_provider_object(base, base_value)
        existing_review = _existing_review_command(base_spec)
        if existing_review and existing_review != review_command:
            limitations.append(f"{name}: {base}.review_command already differs; left unchanged")
            continue
        if not existing_review:
            base_spec["review_command"] = review_command
            providers[base] = base_spec
            changes.append(f"moved {name} command into {base}.review_command")
        del providers[name]
        legacy_to_base[name] = base
        changes.append(f"removed legacy review provider {name}")

    if legacy_to_base:
        _rewrite_provider_references(migrated, legacy_to_base, changes)
    return migrated, changes, limitations


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigValidationError("config file must be valid JSON") from exc
    if not isinstance(loaded, dict):
        raise ConfigValidationError("config root must be an object")
    return loaded


def _legacy_review_base(name: object) -> str | None:
    if not isinstance(name, str):
        return None
    for suffix in LEGACY_REVIEW_SUFFIXES:
        if name.endswith(suffix):
            base = name[: -len(suffix)]
            if base in _KNOWN_STAGE_PROVIDER_BASES:
                return base
    return None


def _provider_default_command(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str) and command.strip():
            return command
    return None


def _as_provider_object(name: str, value: object) -> dict[str, Any]:
    if isinstance(value, str):
        return {"command": value}
    if isinstance(value, dict):
        return copy.deepcopy(value)
    raise ConfigValidationError(f"provider {name} must be a command string or object")


def _existing_review_command(spec: dict[str, Any]) -> str | None:
    value = spec.get("review_command")
    if isinstance(value, str) and value.strip():
        return value
    commands = spec.get("commands")
    if isinstance(commands, dict):
        for key, command in commands.items():
            if str(key).strip().upper() == "REVIEW" and isinstance(command, str) and command.strip():
                return command
    return None


def _rewrite_provider_references(data: dict[str, Any], mapping: dict[str, str], changes: list[str]) -> None:
    routing = data.get("routing")
    if isinstance(routing, dict):
        stage_routes = routing.get("stage_routes")
        if isinstance(stage_routes, dict):
            for stage, provider in list(stage_routes.items()):
                if provider in mapping:
                    stage_routes[stage] = mapping[provider]
                    changes.append(f"rewrote routing.stage_routes.{stage} from {provider} to {mapping[provider]}")
        pools = routing.get("pools")
        if isinstance(pools, dict):
            for stage, names in list(pools.items()):
                if not isinstance(names, list):
                    continue
                rewritten = _rewrite_provider_list(names, mapping)
                if rewritten != names:
                    pools[stage] = rewritten
                    changes.append(f"rewrote routing.pools.{stage} legacy review provider references")
        single = routing.get("single_agent_provider")
        if single in mapping:
            routing["single_agent_provider"] = mapping[single]
            changes.append(f"rewrote routing.single_agent_provider from {single} to {mapping[single]}")
        weights = routing.get("provider_weights")
        if isinstance(weights, dict):
            for legacy, base in mapping.items():
                if legacy in weights:
                    weights.pop(legacy)
                    changes.append(f"removed routing.provider_weights.{legacy}")

    diagnosis = data.get("diagnosis")
    if isinstance(diagnosis, dict):
        provider = diagnosis.get("provider")
        if provider in mapping:
            diagnosis["provider"] = mapping[provider]
            changes.append(f"rewrote diagnosis.provider from {provider} to {mapping[provider]}")


def _rewrite_provider_list(names: list[Any], mapping: dict[str, str]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for name in names:
        rewritten = mapping.get(name, name)
        if isinstance(rewritten, str):
            if rewritten in seen:
                continue
            seen.add(rewritten)
        result.append(rewritten)
    return result


def _next_backup_path(path: Path) -> Path:
    candidate = path.with_name(path.name + ".bak")
    if not candidate.exists():
        return candidate
    index = 1
    while True:
        numbered = path.with_name(f"{path.name}.bak.{index}")
        if not numbered.exists():
            return numbered
        index += 1
