"""Project-local capability registry with fail-closed provider adapters.

The registry is inventory and policy, not an installer.  A project may declare
local capability resources, but StageMesh attaches only capability kinds for
which it has an explicit adapter.  Today that is a read-only ``SKILL.md``
adapter, a skills-only Claude plugin import adapter, and a bounded Codex stdio
MCP adapter. Executable Claude plugin components and MCP use by other providers
remain refused until an equivalent audited adapter exists.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .claude_plugin_capabilities import ClaudePluginCapabilityError, load_claude_plugin
from .mcp_capabilities import McpCapabilityError, load_mcp_manifest

REGISTRY_PATH = Path(".stagemesh") / "capabilities.json"
ATTACHMENT_REQUIRED = "attachment-required"
OBSERVED_LOAD_REQUIRED = "observed-load-required"
SKILL_EVIDENCE_MODES = frozenset({ATTACHMENT_REQUIRED, OBSERVED_LOAD_REQUIRED})
CAPABILITY_KINDS = frozenset({"skill", "mcp", "claude-plugin"})
CAPABILITY_STAGES = frozenset({"IMPLEMENT", "REVIEW"})
CAPABILITY_PERMISSIONS = frozenset(
    {"workspace-read", "workspace-write", "network", "subprocess", "secrets", "home"}
)
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


class CapabilityRegistryError(RuntimeError):
    """The declared registry or a requested capability cannot be used safely."""


@dataclass(frozen=True)
class CapabilityDefinition:
    id: str
    kind: str
    source: str
    enabled: bool
    providers: tuple[str, ...]
    stages: tuple[str, ...]
    permissions: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityAttachment:
    id: str
    path: Path
    source: str
    permissions: tuple[str, ...]
    content_sha256: str
    kind: str = "skill"
    runtime: dict[str, object] | None = None

    def audit_payload(self) -> dict[str, object]:
        return {
            "capability_id": self.id,
            "kind": self.kind,
            "origin": "project-registry",
            "source": self.source,
            "content_sha256": self.content_sha256,
            "permissions": list(self.permissions),
            "selection": "contract",
            "status": "configured" if self.kind == "mcp" else "attached",
            # Prompt delivery proves attachment, not that a model read or followed it.
            "invocation": "unverified",
            # Internal dispatch metadata. Audit persistence strips private keys.
            "_path": str(self.path.resolve()),
            **(
                {"_mcp": self.runtime}
                if self.runtime is not None and self.kind == "mcp"
                else {"_plugin": self.runtime}
                if self.runtime is not None and self.kind == "claude-plugin"
                else {}
            ),
        }


def load_registry(project: Path) -> tuple[CapabilityDefinition, ...]:
    """Load a project's declarations.  A missing registry is an empty registry."""
    root = Path(project).resolve()
    payload = _registry_payload(root)
    if payload is None:
        return ()
    entries = payload.get("capabilities")
    if not isinstance(entries, list):
        raise CapabilityRegistryError("capability registry capabilities must be a list")
    definitions = tuple(_definition(root, entry) for entry in entries)
    ids = [item.id for item in definitions]
    if len(ids) != len(set(ids)):
        raise CapabilityRegistryError("capability registry ids must be unique")
    return definitions


def skill_evidence_mode(project: Path) -> str:
    """Return the project's explicit skill-load evidence policy."""
    payload = _registry_payload(Path(project).resolve())
    if payload is None:
        return ATTACHMENT_REQUIRED
    return str(payload.get("skill_evidence_mode", ATTACHMENT_REQUIRED))


def _registry_payload(root: Path) -> dict[str, object] | None:
    path = root / REGISTRY_PATH
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityRegistryError(f"cannot load {REGISTRY_PATH.as_posix()}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CapabilityRegistryError("capability registry root must be an object")
    if set(payload) - {"format_version", "capabilities", "skill_evidence_mode"}:
        raise CapabilityRegistryError("capability registry contains unsupported fields")
    if payload.get("format_version") != 1:
        raise CapabilityRegistryError("capability registry format_version must be 1")
    if not isinstance(payload.get("capabilities"), list):
        raise CapabilityRegistryError("capability registry capabilities must be a list")
    mode = payload.get("skill_evidence_mode", ATTACHMENT_REQUIRED)
    if mode not in SKILL_EVIDENCE_MODES:
        raise CapabilityRegistryError(
            "capability registry skill_evidence_mode must be attachment-required or observed-load-required"
        )
    return payload


def inventory(project: Path) -> list[dict[str, object]]:
    """Distinguish declared, enabled, installed, adapter-supported and usable."""
    root = Path(project).resolve()
    rows: list[dict[str, object]] = []
    for item in load_registry(root):
        target = _source_path(root, item.source)
        installed = _installed(item, target)
        supported_providers = (
            list(item.providers)
            if item.kind == "skill"
            else ["codex"]
            if item.kind == "mcp"
            else ["claude"]
        )
        adapter_supported = bool(supported_providers)
        configuration_valid = installed
        configuration_error = None
        runtime_installed = installed
        if installed and item.kind == "mcp":
            try:
                config = load_mcp_manifest(target, root)
                runtime_installed = shutil.which(config.command) is not None
            except McpCapabilityError as exc:
                configuration_valid = False
                runtime_installed = False
                configuration_error = str(exc)
        if installed and item.kind == "claude-plugin":
            try:
                load_claude_plugin(target, root)
            except ClaudePluginCapabilityError as exc:
                configuration_valid = False
                runtime_installed = False
                configuration_error = str(exc)
        available = installed and adapter_supported and configuration_valid and runtime_installed
        rows.append(
            {
                "id": item.id,
                "kind": item.kind,
                "source": item.source,
                "declared": True,
                "enabled": item.enabled,
                "installed": installed,
                "adapter_supported": adapter_supported,
                "supported_providers": supported_providers,
                "configuration_valid": configuration_valid,
                "configuration_error": configuration_error,
                "runtime_installed": runtime_installed,
                "available": available,
                "usable": item.enabled and available,
                "providers": list(item.providers),
                "stages": list(item.stages),
                "permissions": list(item.permissions),
                "invocation": "not-observed",
            }
        )
    return rows


def resolve_attachments(
    project: Path,
    requested: tuple[str, ...] | list[str],
    *,
    provider: str,
    stage: str,
) -> tuple[CapabilityAttachment, ...]:
    """Resolve explicitly requested capabilities for one provider and stage."""
    if not requested:
        return ()
    root = Path(project).resolve()
    definitions = {item.id: item for item in load_registry(root)}
    normalized_stage = str(stage).upper()
    if normalized_stage not in CAPABILITY_STAGES:
        raise CapabilityRegistryError(f"unsupported capability stage: {stage}")
    attachments: list[CapabilityAttachment] = []
    seen: set[str] = set()
    for raw in requested:
        capability_id = str(raw).strip()
        if capability_id in seen:
            continue
        seen.add(capability_id)
        item = definitions.get(capability_id)
        if item is None:
            raise CapabilityRegistryError(
                f"requested capability is not registered: {capability_id}"
            )
        if not item.enabled:
            raise CapabilityRegistryError(f"requested capability is disabled: {capability_id}")
        if provider not in item.providers:
            raise CapabilityRegistryError(
                f"capability {capability_id} is not compatible with provider {provider}"
            )
        if normalized_stage not in item.stages:
            raise CapabilityRegistryError(
                f"capability {capability_id} is not enabled for stage {normalized_stage}"
            )
        target = _source_path(root, item.source)
        if not _installed(item, target):
            raise CapabilityRegistryError(f"requested capability is not installed: {capability_id}")
        runtime = None
        if item.kind == "mcp":
            if provider != "codex":
                raise CapabilityRegistryError(
                    f"capability {capability_id} has no bounded MCP adapter for provider {provider}"
                )
            try:
                config = load_mcp_manifest(target, root)
            except McpCapabilityError as exc:
                raise CapabilityRegistryError(
                    f"requested capability {capability_id} has invalid MCP configuration: {exc}"
                ) from exc
            if shutil.which(config.command) is None:
                raise CapabilityRegistryError(
                    f"requested capability {capability_id} server executable is not installed"
                )
            runtime = config.private_payload()
        if item.kind == "claude-plugin":
            if provider != "claude":
                raise CapabilityRegistryError(
                    f"capability {capability_id} has no Claude plugin adapter for provider {provider}"
                )
            try:
                plugin = load_claude_plugin(target, root)
            except ClaudePluginCapabilityError as exc:
                raise CapabilityRegistryError(
                    f"requested capability {capability_id} has invalid Claude plugin configuration: {exc}"
                ) from exc
            runtime = plugin.private_payload()
        attachments.append(
            CapabilityAttachment(
                id=item.id,
                path=target,
                source=item.source,
                permissions=item.permissions,
                content_sha256=(
                    plugin.content_sha256
                    if item.kind == "claude-plugin"
                    else hashlib.sha256(target.read_bytes()).hexdigest()
                ),
                kind=item.kind,
                runtime=runtime,
            )
        )
    return tuple(attachments)


def requested_for_stage(
    project: Path,
    requested: tuple[str, ...] | list[str],
    *,
    stage: str,
) -> tuple[str, ...]:
    """Return contract-requested ids that apply to this stage.

    A contract owns one capability list for its whole lifecycle.  Stage policy
    belongs to each registry entry, so an implementation-only capability must
    not make review fail (and a review-only capability must not block
    implementation).  Unknown ids remain a contract error at every stage.
    """
    if not requested:
        return ()
    normalized_stage = str(stage).upper()
    if normalized_stage not in CAPABILITY_STAGES:
        raise CapabilityRegistryError(f"unsupported capability stage: {stage}")
    definitions = {item.id: item for item in load_registry(Path(project).resolve())}
    selected: list[str] = []
    seen: set[str] = set()
    for raw in requested:
        capability_id = str(raw).strip()
        if capability_id in seen:
            continue
        seen.add(capability_id)
        item = definitions.get(capability_id)
        if item is None:
            raise CapabilityRegistryError(
                f"requested capability is not registered: {capability_id}"
            )
        if normalized_stage in item.stages:
            selected.append(capability_id)
    return tuple(selected)


def task_usage(
    database: Path,
    task_id: str,
) -> tuple[bool, bool, dict[str, dict[str, object]]]:
    """Read durable capability observations for one task without creating state.

    The booleans report database availability and task existence.  Only
    privacy-bounded audit payload fields are projected; raw provider events,
    prompts, paths, arguments and results are never read into the report.
    """
    task = str(task_id).strip()
    if not task:
        raise CapabilityRegistryError("capability usage task id must be non-empty")
    path = Path(database)
    if not path.is_file():
        return False, False, {}
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('tasks','audit_events')"
            )
        }
        if "audit_events" not in tables:
            return True, False, {}
        task_exists = bool(
            "tasks" in tables
            and connection.execute("SELECT 1 FROM tasks WHERE id=?", (task,)).fetchone()
        )
        rows = connection.execute(
            "SELECT event_type, payload, created_at FROM audit_events "
            "WHERE event_type IN ('capability.attached','capability.loaded','capability.invoked',"
            "'capability.evidence_missing') "
            "AND json_valid(payload) "
            "AND json_extract(payload, '$.task_id')=? "
            "ORDER BY created_at, rowid",
            (task,),
        )
        usage: dict[str, dict[str, object]] = {}
        for row in rows:
            try:
                payload = json.loads(str(row["payload"]))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            event_type = str(row["event_type"])
            ids: list[str] = []
            if event_type == "capability.evidence_missing":
                missing = payload.get("missing_capabilities")
                if isinstance(missing, list):
                    ids = [str(item) for item in missing if isinstance(item, str) and item]
                field = "evidence_missing"
            else:
                capability_id = payload.get("capability_id")
                if isinstance(capability_id, str) and capability_id:
                    ids = [capability_id]
                field = event_type.rsplit(".", 1)[-1]
            for capability_id in ids:
                item = usage.setdefault(
                    capability_id,
                    {
                        "attached": 0,
                        "loaded": 0,
                        "invoked": 0,
                        "evidence_missing": 0,
                        "providers": set(),
                        "stages": set(),
                        "last_observed_at": None,
                    },
                )
                item[field] = int(item[field]) + 1
                provider = payload.get("provider")
                stage = payload.get("stage")
                if isinstance(provider, str) and provider:
                    item["providers"].add(provider)  # type: ignore[union-attr]
                if isinstance(stage, str) and stage:
                    item["stages"].add(stage)  # type: ignore[union-attr]
                item["last_observed_at"] = float(row["created_at"])
        projected: dict[str, dict[str, object]] = {}
        for capability_id, item in usage.items():
            invoked = int(item["invoked"])
            loaded = int(item["loaded"])
            attached = int(item["attached"])
            status = (
                "invoked"
                if invoked
                else "loaded"
                if loaded
                else "attached-unverified"
                if attached
                else "not-observed"
            )
            projected[capability_id] = {
                "status": status,
                "attached": attached,
                "loaded": loaded,
                "invoked": invoked,
                "evidence_missing": int(item["evidence_missing"]),
                "providers": sorted(item["providers"]),  # type: ignore[arg-type]
                "stages": sorted(item["stages"]),  # type: ignore[arg-type]
                "last_observed_at": item["last_observed_at"],
            }
        return True, task_exists, projected
    except sqlite3.DatabaseError as exc:
        raise CapabilityRegistryError(f"cannot read capability usage evidence: {exc}") from exc
    finally:
        connection.close()


def _definition(root: Path, raw: object) -> CapabilityDefinition:
    if not isinstance(raw, dict):
        raise CapabilityRegistryError("each capability must be an object")
    allowed = {"id", "kind", "source", "enabled", "providers", "stages", "permissions"}
    if set(raw) - allowed:
        raise CapabilityRegistryError("capability contains unsupported fields")
    capability_id = raw.get("id")
    if not isinstance(capability_id, str) or not _ID.fullmatch(capability_id):
        raise CapabilityRegistryError("capability id must be a lowercase portable identifier")
    kind = raw.get("kind")
    if kind not in CAPABILITY_KINDS:
        raise CapabilityRegistryError(
            f"capability {capability_id} kind must be one of {', '.join(sorted(CAPABILITY_KINDS))}"
        )
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise CapabilityRegistryError(f"capability {capability_id} source is required")
    source = source.strip().replace("\\", "/")
    _source_path(root, source)
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise CapabilityRegistryError(f"capability {capability_id} enabled must be a boolean")
    providers = _names(raw.get("providers"), f"capability {capability_id} providers")
    stages = _names(raw.get("stages"), f"capability {capability_id} stages")
    invalid_stages = set(stages) - CAPABILITY_STAGES
    if invalid_stages:
        raise CapabilityRegistryError(
            f"capability {capability_id} has unsupported stages: {', '.join(sorted(invalid_stages))}"
        )
    permissions = _names(
        raw.get("permissions", ["workspace-read"] if kind == "skill" else []),
        f"capability {capability_id} permissions",
        allow_empty=kind != "skill",
    )
    invalid_permissions = set(permissions) - CAPABILITY_PERMISSIONS
    if invalid_permissions:
        raise CapabilityRegistryError(
            f"capability {capability_id} has unsupported permissions: {', '.join(sorted(invalid_permissions))}"
        )
    if kind == "skill" and set(permissions) != {"workspace-read"}:
        raise CapabilityRegistryError(
            f"skill capability {capability_id} may request only workspace-read"
        )
    if kind == "mcp" and set(permissions) != {"workspace-read", "subprocess"}:
        raise CapabilityRegistryError(
            f"MCP capability {capability_id} currently requires exactly workspace-read and subprocess"
        )
    if kind == "mcp" and set(stages) != {"IMPLEMENT"}:
        raise CapabilityRegistryError(
            f"MCP capability {capability_id} currently supports only the IMPLEMENT stage"
        )
    if kind == "claude-plugin" and set(providers) != {"claude"}:
        raise CapabilityRegistryError(
            f"claude-plugin capability {capability_id} must declare only the claude provider"
        )
    if kind == "claude-plugin" and set(permissions) != {"workspace-read"}:
        raise CapabilityRegistryError(
            f"claude-plugin capability {capability_id} may request only workspace-read"
        )
    return CapabilityDefinition(
        id=capability_id,
        kind=kind,
        source=source,
        enabled=enabled,
        providers=providers,
        stages=stages,
        permissions=permissions,
    )


def _names(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise CapabilityRegistryError(
            f"{field} must be a {'list' if allow_empty else 'non-empty list'}"
        )
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise CapabilityRegistryError(f"{field} must contain non-empty strings")
    names = tuple(item.strip() for item in value)
    if len(names) != len(set(names)):
        raise CapabilityRegistryError(f"{field} must not contain duplicates")
    return names


def _source_path(root: Path, source: str) -> Path:
    relative = Path(source)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise CapabilityRegistryError(
            "capability source must be a relative path inside the project"
        )
    target = (root / relative).resolve()
    try:
        inside = target.is_relative_to(root)
    except ValueError:
        inside = False
    if not inside:
        raise CapabilityRegistryError("capability source must stay inside the project")
    return target


def _installed(item: CapabilityDefinition, target: Path) -> bool:
    if item.kind == "skill":
        return target.is_file() and target.name == "SKILL.md"
    if item.kind == "claude-plugin":
        return target.is_dir()
    return target.exists()
