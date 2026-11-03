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
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, replace
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
    content_sha256: str | None = None


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
    definitions, _mode = _load_registry_state(Path(project).resolve())
    return definitions


def _load_registry_state(
    root: Path,
) -> tuple[tuple[CapabilityDefinition, ...], str]:
    """Load declarations and evidence mode from one validated registry read."""
    payload = _registry_payload(root)
    if payload is None:
        return (), ATTACHMENT_REQUIRED
    entries = payload.get("capabilities")
    if not isinstance(entries, list):
        raise CapabilityRegistryError("capability registry capabilities must be a list")
    definitions = tuple(_definition(root, entry) for entry in entries)
    ids = [item.id for item in definitions]
    if len(ids) != len(set(ids)):
        raise CapabilityRegistryError("capability registry ids must be unique")
    return definitions, str(payload.get("skill_evidence_mode", ATTACHMENT_REQUIRED))


def capability_policy_snapshot(
    project: Path, requested: tuple[str, ...] | list[str]
) -> dict[str, object]:
    """Return the deterministic, privacy-bounded policy for selected declarations."""
    loaded, evidence_mode = _load_registry_state(Path(project).resolve())
    definitions = {item.id: item for item in loaded}
    requested_ids = list(
        dict.fromkeys(str(item).strip() for item in requested if str(item).strip())
    )
    missing = [item for item in requested_ids if item not in definitions]
    if missing:
        raise CapabilityRegistryError(
            "requested capability is not registered: " + ", ".join(missing)
        )
    selected = [
        _definition_payload(definitions[capability_id])
        for capability_id in sorted(requested_ids)
    ]
    canonical_json = json.dumps(
        {
            "format_version": 1,
            "skill_evidence_mode": evidence_mode,
            "capabilities": selected,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(canonical_json) > 10000:
        raise CapabilityRegistryError("selected capability policy exceeds the 10000 character limit")
    return {
        "digest": hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
        "canonical_json": canonical_json,
        "capability_ids": sorted(requested_ids),
        "skill_evidence_mode": evidence_mode,
    }


def capability_policy_report(
    project: Path,
    connection: sqlite3.Connection | None,
    task_id: str,
    requested: tuple[str, ...] | list[str],
) -> dict[str, object]:
    """Compare the current selected policy with the immutable task binding."""
    requested_ids = list(
        dict.fromkeys(str(item).strip() for item in requested if str(item).strip())
    )
    try:
        current = capability_policy_snapshot(project, requested_ids)
    except (CapabilityRegistryError, OSError) as exc:
        current = None
        current_error = str(exc)[:300]
    else:
        current_error = None
    try:
        table = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_capability_policies'"
            ).fetchone()
            if connection is not None
            else None
        )
        row = (
            connection.execute(
                "SELECT digest, canonical_json FROM task_capability_policies WHERE task_id=?",
                (str(task_id),),
            ).fetchone()
            if connection is not None and table is not None
            else None
        )
    except sqlite3.Error as exc:
        return {
            "state": "invalid",
            "requested": requested_ids,
            "frozen_digest": None,
            "current_digest": current["digest"] if current else None,
            "frozen_skill_evidence_mode": None,
            "current_skill_evidence_mode": (
                current["skill_evidence_mode"] if current else None
            ),
            "rebind_required": bool(requested_ids),
            "dispatch_ready": not requested_ids,
            "reason": f"cannot read frozen capability policy: {exc}"[:300],
        }
    if not requested_ids:
        return {
            "state": "not-required",
            "requested": [],
            "frozen_digest": str(row["digest"]) if row is not None else None,
            "current_digest": current["digest"] if current else None,
            "frozen_skill_evidence_mode": None,
            "current_skill_evidence_mode": (
                current["skill_evidence_mode"] if current else None
            ),
            "rebind_required": False,
            "dispatch_ready": True,
            "reason": current_error,
        }
    if row is None:
        return {
            "state": "legacy-unbound",
            "requested": requested_ids,
            "frozen_digest": None,
            "current_digest": current["digest"] if current else None,
            "frozen_skill_evidence_mode": None,
            "current_skill_evidence_mode": (
                current["skill_evidence_mode"] if current else None
            ),
            "rebind_required": True,
            "dispatch_ready": False,
            "reason": "selected capabilities are not frozen with this task contract",
        }
    frozen_digest = str(row["digest"])
    canonical_json = str(row["canonical_json"])
    actual_frozen = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    try:
        frozen_payload = json.loads(canonical_json)
        frozen_valid = (
            isinstance(frozen_payload, dict)
            and frozen_payload.get("format_version") == 1
            and isinstance(frozen_payload.get("capabilities"), list)
            and frozen_payload.get("skill_evidence_mode") in SKILL_EVIDENCE_MODES
            and re.fullmatch(r"[0-9a-f]{64}", frozen_digest) is not None
            and actual_frozen == frozen_digest
        )
    except json.JSONDecodeError:
        frozen_payload = {}
        frozen_valid = False
    frozen_mode = (
        str(frozen_payload.get("skill_evidence_mode"))
        if isinstance(frozen_payload, dict)
        and frozen_payload.get("skill_evidence_mode") in SKILL_EVIDENCE_MODES
        else None
    )
    if not frozen_valid:
        return {
            "state": "invalid",
            "requested": requested_ids,
            "frozen_digest": frozen_digest[:64],
            "current_digest": current["digest"] if current else None,
            "frozen_skill_evidence_mode": frozen_mode,
            "current_skill_evidence_mode": (
                current["skill_evidence_mode"] if current else None
            ),
            "rebind_required": True,
            "dispatch_ready": False,
            "reason": "frozen capability policy is invalid",
        }
    if current is None:
        missing_selected = bool(
            current_error
            and current_error.startswith("requested capability is not registered:")
        )
        return {
            "state": "drifted" if missing_selected else "invalid",
            "requested": requested_ids,
            "frozen_digest": frozen_digest,
            "current_digest": None,
            "frozen_skill_evidence_mode": frozen_mode,
            "current_skill_evidence_mode": None,
            "rebind_required": True,
            "dispatch_ready": False,
            "reason": current_error,
        }
    verified = frozen_digest == current["digest"]
    drift_reason = (
        "skill evidence mode changed"
        if frozen_mode != current["skill_evidence_mode"]
        else "selected capability declaration policy changed"
    )
    return {
        "state": "verified" if verified else "drifted",
        "requested": requested_ids,
        "frozen_digest": frozen_digest,
        "current_digest": current["digest"],
        "frozen_skill_evidence_mode": frozen_mode,
        "current_skill_evidence_mode": current["skill_evidence_mode"],
        "rebind_required": not verified,
        "dispatch_ready": verified,
        "reason": None if verified else drift_reason,
    }


def require_capability_policy(
    project: Path,
    connection: sqlite3.Connection,
    task_id: str,
    requested: tuple[str, ...] | list[str],
) -> dict[str, object]:
    """Fail closed before launch unless the current policy matches its frozen binding."""
    report = capability_policy_report(project, connection, task_id, requested)
    if report["dispatch_ready"]:
        return report
    expected = report.get("frozen_digest") or "missing"
    observed = report.get("current_digest") or "unavailable"
    raise CapabilityRegistryError(
        f"capability policy {report['state']} for task {task_id}: "
        f"expected={expected} observed={observed}; run rebind-contract --plan"
    )


def skill_evidence_mode(project: Path) -> str:
    """Return the project's explicit skill-load evidence policy."""
    _definitions, mode = _load_registry_state(Path(project).resolve())
    return mode


def plan_skill_evidence_mode(project: Path, mode: str) -> dict[str, object]:
    """Validate a project skill-evidence policy change without writing it."""
    plan, _payload = _skill_evidence_mode_change(project, mode)
    return plan


def set_skill_evidence_mode(project: Path, mode: str) -> dict[str, object]:
    """Atomically apply an explicit project skill-evidence policy."""
    plan, payload = _skill_evidence_mode_change(project, mode)
    if plan["changed"]:
        _write_registry(Path(project).resolve(), payload)
    return {
        **plan,
        "planned": False,
        "applied": True,
        "writes": list(plan["planned_writes"]) if plan["changed"] else [],
        "written": bool(plan["changed"]),
    }


def _registry_payload(root: Path) -> dict[str, object] | None:
    path = root / REGISTRY_PATH
    if path.is_symlink():
        raise CapabilityRegistryError("capability registry read refuses a symlink target")
    if path.parent.is_symlink():
        raise CapabilityRegistryError("capability registry read refuses a symlink directory")
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
    return [_inventory_row(root, item) for item in load_registry(root)]


def plan_capability_registration(
    project: Path,
    *,
    capability_id: str,
    kind: str,
    source: str,
    providers: list[str],
    stages: list[str],
    permissions: list[str],
    enabled: bool = False,
    update: bool = False,
) -> dict[str, object]:
    """Validate one project-local registry addition without writing anything."""
    plan, _payload = _registration_change(
        project,
        capability_id=capability_id,
        kind=kind,
        source=source,
        providers=providers,
        stages=stages,
        permissions=permissions,
        enabled=enabled,
        update=update,
    )
    return plan


def register_capability(
    project: Path,
    *,
    capability_id: str,
    kind: str,
    source: str,
    providers: list[str],
    stages: list[str],
    permissions: list[str],
    enabled: bool = False,
    update: bool = False,
) -> dict[str, object]:
    """Atomically register one already-present project capability."""
    plan, payload = _registration_change(
        project,
        capability_id=capability_id,
        kind=kind,
        source=source,
        providers=providers,
        stages=stages,
        permissions=permissions,
        enabled=enabled,
        update=update,
    )
    if plan["changed"]:
        _write_registry(Path(project).resolve(), payload)
    return {
        **plan,
        "planned": False,
        "applied": True,
        "writes": list(plan["planned_writes"]) if plan["changed"] else [],
        "written": bool(plan["changed"]),
    }


def plan_capability_enabled(project: Path, capability_id: str, enabled: bool) -> dict[str, object]:
    """Validate one explicit enable/disable change without writing it."""
    plan, _payload = _enabled_change(project, capability_id, enabled)
    return plan


def plan_capability_pin(project: Path, capability_id: str) -> dict[str, object]:
    """Preview pinning one declaration to its current validated source content."""
    plan, _payload = _pin_change(project, capability_id)
    return plan


def set_capability_pin(project: Path, capability_id: str) -> dict[str, object]:
    """Atomically pin one declaration to its current validated source content."""
    plan, payload = _pin_change(project, capability_id)
    if plan["changed"]:
        _write_registry(Path(project).resolve(), payload)
    return {
        **plan,
        "planned": False,
        "applied": True,
        "writes": list(plan["planned_writes"]) if plan["changed"] else [],
        "written": bool(plan["changed"]),
    }


def set_capability_enabled(project: Path, capability_id: str, enabled: bool) -> dict[str, object]:
    """Atomically enable or disable one declaration without attaching or invoking it."""
    plan, payload = _enabled_change(project, capability_id, enabled)
    if plan["changed"]:
        _write_registry(Path(project).resolve(), payload)
    return {
        **plan,
        "planned": False,
        "applied": True,
        "writes": list(plan["planned_writes"]) if plan["changed"] else [],
        "written": bool(plan["changed"]),
    }


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
        observed_digest = (
            plugin.content_sha256
            if item.kind == "claude-plugin"
            else hashlib.sha256(target.read_bytes()).hexdigest()
        )
        if item.content_sha256 is not None and item.content_sha256 != observed_digest:
            raise CapabilityRegistryError(
                f"requested capability source digest drifted: {capability_id}; "
                f"expected {item.content_sha256}, observed {observed_digest}"
            )
        attachments.append(
            CapabilityAttachment(
                id=item.id,
                path=target,
                source=item.source,
                permissions=item.permissions,
                content_sha256=observed_digest,
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
        task_exists, projected = task_usage_from_connection(connection, task)
        return True, task_exists, projected
    except sqlite3.DatabaseError as exc:
        raise CapabilityRegistryError(f"cannot read capability usage evidence: {exc}") from exc
    finally:
        connection.close()


def task_usage_from_connection(
    connection: sqlite3.Connection, task_id: str
) -> tuple[bool, dict[str, dict[str, object]]]:
    """Project privacy-bounded usage through an existing Store connection."""
    task = str(task_id).strip()
    if not task:
        raise CapabilityRegistryError("capability usage task id must be non-empty")
    tables = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('tasks','audit_events')"
        )
    }
    if "audit_events" not in tables:
        return False, {}
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
            payload = json.loads(str(row["payload"] if isinstance(row, sqlite3.Row) else row[1]))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(row["event_type"] if isinstance(row, sqlite3.Row) else row[0])
        created_at = row["created_at"] if isinstance(row, sqlite3.Row) else row[2]
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
                    "content_sha256s": set(),
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
            content_sha256 = payload.get("content_sha256")
            if (
                isinstance(content_sha256, str)
                and re.fullmatch(r"[0-9a-f]{64}", content_sha256)
            ):
                item["content_sha256s"].add(content_sha256)  # type: ignore[union-attr]
            item["last_observed_at"] = float(created_at)
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
            else "evidence-missing"
            if int(item["evidence_missing"])
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
            "content_sha256s": sorted(item["content_sha256s"]),  # type: ignore[arg-type]
            "last_observed_at": item["last_observed_at"],
        }
    return task_exists, projected


def task_capability_summary(
    project: Path,
    connection: sqlite3.Connection,
    task_id: str,
    *,
    requested: tuple[str, ...] | list[str] = (),
) -> dict[str, object]:
    """Combine registry state and durable observations for one task.

    This is the canonical privacy-bounded projection used by operator reports.
    It never reads prompts, provider output, tool arguments or result payloads.
    Historical observations remain visible when a declaration was removed, and
    requested-but-unregistered ids remain explicit instead of disappearing.
    """
    task_exists, observed = task_usage_from_connection(connection, task_id)
    requested_ids = list(dict.fromkeys(str(item).strip() for item in requested if str(item).strip()))
    rows = {str(row["id"]): row for row in inventory(project)}
    capability_ids: list[str] = []
    for capability_id in (*requested_ids, *observed):
        if capability_id not in capability_ids:
            capability_ids.append(capability_id)
    capabilities: list[dict[str, object]] = []
    for capability_id in capability_ids:
        usage = observed.get(capability_id, _empty_usage())
        row = rows.get(capability_id)
        if row is None:
            projected: dict[str, object] = {
                "id": capability_id,
                "registered": False,
                "requested": capability_id in requested_ids,
                "invocation": usage["status"],
                "usage": usage,
            }
        else:
            current_digest = row["observed_content_sha256"]
            evidence_digests = usage["content_sha256s"]
            evidence_matches_current = (
                None
                if not evidence_digests
                else isinstance(current_digest, str) and current_digest in evidence_digests
            )
            invocation = usage["status"]
            if invocation in {"loaded", "invoked"} and evidence_matches_current is not True:
                invocation = "evidence-digest-mismatch"
            projected = {
                **{
                    key: row[key]
                    for key in (
                        "id",
                        "kind",
                        "source",
                        "enabled",
                        "installed",
                        "adapter_supported",
                        "supported_providers",
                        "runtime_installed",
                        "available",
                        "usable",
                        "providers",
                        "stages",
                        "permissions",
                        "content_sha256",
                        "observed_content_sha256",
                        "integrity",
                    )
                },
                "registered": True,
                "requested": capability_id in requested_ids,
                "invocation": invocation,
                "evidence_matches_current": evidence_matches_current,
                "usage": usage,
            }
        capabilities.append(projected)
    return {
        "task_id": str(task_id),
        "history_available": True,
        "task_exists": task_exists,
        "policy_binding": capability_policy_report(
            project, connection, task_id, requested_ids
        ),
        "skill_evidence_mode": skill_evidence_mode(project),
        "registry_count": len(rows),
        "requested": requested_ids,
        "unregistered_requested": [item for item in requested_ids if item not in rows],
        "capabilities": capabilities,
    }


def _empty_usage() -> dict[str, object]:
    return {
        "status": "not-observed",
        "attached": 0,
        "loaded": 0,
        "invoked": 0,
        "evidence_missing": 0,
        "providers": [],
        "stages": [],
        "content_sha256s": [],
        "last_observed_at": None,
    }


def _inventory_row(root: Path, item: CapabilityDefinition) -> dict[str, object]:
    target = _source_path(root, item.source)
    installed = _installed(item, target)
    supported_providers = (
        list(item.providers)
        if item.kind == "skill"
        else ["codex"]
        if item.kind == "mcp"
        else ["claude"]
    )
    adapter_supported = any(provider in supported_providers for provider in item.providers)
    configuration_valid = installed
    configuration_error = None
    runtime_installed = installed
    observed_content_sha256 = None
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
            plugin = load_claude_plugin(target, root)
            observed_content_sha256 = plugin.content_sha256
        except ClaudePluginCapabilityError as exc:
            configuration_valid = False
            runtime_installed = False
            configuration_error = str(exc)
    if installed and item.kind != "claude-plugin":
        try:
            observed_content_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError as exc:
            configuration_valid = False
            runtime_installed = False
            configuration_error = str(exc)
    integrity = (
        "unavailable"
        if observed_content_sha256 is None
        else "unpinned"
        if item.content_sha256 is None
        else "verified"
        if item.content_sha256 == observed_content_sha256
        else "drifted"
    )
    available = (
        installed
        and adapter_supported
        and configuration_valid
        and runtime_installed
        and integrity != "drifted"
    )
    return {
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
        "content_sha256": item.content_sha256,
        "observed_content_sha256": observed_content_sha256,
        "integrity": integrity,
        "invocation": "not-observed",
    }


def _definition_payload(item: CapabilityDefinition) -> dict[str, object]:
    payload = {
        "id": item.id,
        "kind": item.kind,
        "source": item.source,
        "enabled": item.enabled,
        "providers": list(item.providers),
        "stages": list(item.stages),
        "permissions": list(item.permissions),
    }
    if item.content_sha256 is not None:
        payload["content_sha256"] = item.content_sha256
    return payload


def _registration_change(
    project: Path,
    *,
    capability_id: str,
    kind: str,
    source: str,
    providers: list[str],
    stages: list[str],
    permissions: list[str],
    enabled: bool,
    update: bool,
) -> tuple[dict[str, object], dict[str, object]]:
    root = Path(project).resolve()
    existing = load_registry(root)
    payload = _registry_payload(root) or {"format_version": 1, "capabilities": []}
    entries = list(payload["capabilities"])
    indexes = {item.id: index for index, item in enumerate(existing)}
    index = indexes.get(str(capability_id))
    if index is not None and not update:
        raise CapabilityRegistryError(
            f"capability is already registered: {capability_id}; use an explicit update"
        )
    if index is None and update:
        raise CapabilityRegistryError(f"cannot update unregistered capability: {capability_id}")
    raw = {
        "id": capability_id,
        "kind": kind,
        "source": source,
        "enabled": enabled,
        "providers": providers,
        "stages": stages,
        "permissions": permissions,
    }
    item = _definition(root, raw)
    row = _inventory_row(root, item)
    if not row["installed"]:
        raise CapabilityRegistryError(
            f"capability {item.id} source is not installed inside the project"
        )
    if not row["configuration_valid"]:
        raise CapabilityRegistryError(
            f"capability {item.id} configuration is invalid: {row['configuration_error']}"
        )
    observed_digest = row["observed_content_sha256"]
    if not isinstance(observed_digest, str):
        raise CapabilityRegistryError(
            f"capability {item.id} source digest could not be computed"
        )
    item = replace(item, content_sha256=observed_digest)
    entry = _definition_payload(item)
    row = _inventory_row(root, item)
    if enabled and not row["available"]:
        raise CapabilityRegistryError(
            f"capability {item.id} cannot be enabled because it is not available for a declared provider"
        )
    previous = _definition_payload(existing[index]) if index is not None else None
    changed = previous != entry
    if index is None:
        entries.append(entry)
    else:
        entries[index] = entry
    next_payload = {**payload, "capabilities": entries}
    plan = {
        "planned": True,
        "operation": "update" if index is not None else "register",
        "changed": changed,
        "registry": REGISTRY_PATH.as_posix(),
        "entry": entry,
        "inventory": row,
        "planned_writes": [REGISTRY_PATH.as_posix()] if changed else [],
        "writes": [],
        "provider_calls": 0,
        "network_calls": 0,
        "package_installs": 0,
    }
    return plan, next_payload


def _pin_change(
    project: Path, capability_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    root = Path(project).resolve()
    existing = load_registry(root)
    payload = _registry_payload(root)
    if payload is None:
        raise CapabilityRegistryError(f"capability is not registered: {capability_id}")
    entries = list(payload["capabilities"])
    index = next((i for i, item in enumerate(existing) if item.id == capability_id), None)
    if index is None:
        raise CapabilityRegistryError(f"capability is not registered: {capability_id}")
    item = existing[index]
    row = _inventory_row(root, item)
    if not row["installed"] or not row["configuration_valid"]:
        raise CapabilityRegistryError(
            f"capability {item.id} source is not available for pinning"
        )
    observed = row["observed_content_sha256"]
    if not isinstance(observed, str):
        raise CapabilityRegistryError(
            f"capability {item.id} source digest could not be computed"
        )
    raw = dict(entries[index])
    changed = raw.get("content_sha256") != observed
    raw["content_sha256"] = observed
    pinned = _definition(root, raw)
    pinned_row = _inventory_row(root, pinned)
    entries[index] = raw
    next_payload = {**payload, "capabilities": entries}
    plan = {
        "planned": True,
        "operation": "pin",
        "changed": changed,
        "registry": REGISTRY_PATH.as_posix(),
        "entry": raw,
        "inventory": pinned_row,
        "previous_content_sha256": item.content_sha256,
        "content_sha256": observed,
        "planned_writes": [REGISTRY_PATH.as_posix()] if changed else [],
        "writes": [],
        "provider_calls": 0,
        "network_calls": 0,
        "package_installs": 0,
        "database_writes": 0,
    }
    return plan, next_payload


def _enabled_change(
    project: Path, capability_id: str, enabled: bool
) -> tuple[dict[str, object], dict[str, object]]:
    if not isinstance(enabled, bool):
        raise CapabilityRegistryError("capability enabled state must be a boolean")
    root = Path(project).resolve()
    existing = load_registry(root)
    payload = _registry_payload(root)
    if payload is None:
        raise CapabilityRegistryError(f"capability is not registered: {capability_id}")
    entries = list(payload["capabilities"])
    index = next((i for i, item in enumerate(existing) if item.id == capability_id), None)
    if index is None:
        raise CapabilityRegistryError(f"capability is not registered: {capability_id}")
    raw = dict(entries[index])
    changed = raw.get("enabled", False) is not enabled
    raw["enabled"] = enabled
    item = _definition(root, raw)
    row = _inventory_row(root, item)
    if enabled and not row["available"]:
        raise CapabilityRegistryError(
            f"capability {item.id} cannot be enabled because it is not available for a declared provider"
        )
    entries[index] = raw
    next_payload = {**payload, "capabilities": entries}
    plan = {
        "planned": True,
        "operation": "enable" if enabled else "disable",
        "changed": changed,
        "registry": REGISTRY_PATH.as_posix(),
        "entry": raw,
        "inventory": row,
        "planned_writes": [REGISTRY_PATH.as_posix()] if changed else [],
        "writes": [],
        "provider_calls": 0,
        "network_calls": 0,
        "package_installs": 0,
    }
    return plan, next_payload


def _skill_evidence_mode_change(
    project: Path, mode: str
) -> tuple[dict[str, object], dict[str, object]]:
    normalized = str(mode).strip()
    if normalized not in SKILL_EVIDENCE_MODES:
        raise CapabilityRegistryError(
            "skill evidence mode must be attachment-required or observed-load-required"
        )
    root = Path(project).resolve()
    payload = _registry_payload(root)
    current = (
        ATTACHMENT_REQUIRED
        if payload is None
        else str(payload.get("skill_evidence_mode", ATTACHMENT_REQUIRED))
    )
    changed = current != normalized
    next_payload = dict(payload or {"format_version": 1, "capabilities": []})
    if normalized == ATTACHMENT_REQUIRED:
        next_payload.pop("skill_evidence_mode", None)
    else:
        next_payload["skill_evidence_mode"] = normalized
    plan = {
        "planned": True,
        "operation": "set-skill-evidence-mode",
        "changed": changed,
        "registry": REGISTRY_PATH.as_posix(),
        "current_mode": current,
        "skill_evidence_mode": normalized,
        "planned_writes": [REGISTRY_PATH.as_posix()] if changed else [],
        "writes": [],
        "provider_calls": 0,
        "network_calls": 0,
        "package_installs": 0,
        "database_writes": 0,
    }
    return plan, next_payload


def _write_registry(root: Path, payload: dict[str, object]) -> None:
    path = root / REGISTRY_PATH
    parent = path.parent
    if path.is_symlink():
        raise CapabilityRegistryError("capability registry write refuses a symlink target")
    if parent.is_symlink():
        raise CapabilityRegistryError("capability registry write refuses a symlink directory")
    if parent.exists() and not parent.resolve().is_relative_to(root):
        raise CapabilityRegistryError("capability registry directory must stay inside the project")
    descriptor = -1
    temporary: Path | None = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if not parent.resolve().is_relative_to(root):
            raise CapabilityRegistryError(
                "capability registry directory must stay inside the project"
            )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(json.dumps(payload, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CapabilityRegistryError(f"cannot write {REGISTRY_PATH.as_posix()}: {exc}") from exc


def _definition(root: Path, raw: object) -> CapabilityDefinition:
    if not isinstance(raw, dict):
        raise CapabilityRegistryError("each capability must be an object")
    allowed = {
        "id", "kind", "source", "enabled", "providers", "stages", "permissions",
        "content_sha256",
    }
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
    content_sha256 = raw.get("content_sha256")
    if content_sha256 is not None and (
        not isinstance(content_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", content_sha256)
    ):
        raise CapabilityRegistryError(
            f"capability {capability_id} content_sha256 must be a lowercase SHA-256 digest"
        )
    return CapabilityDefinition(
        id=capability_id,
        kind=kind,
        source=source,
        enabled=enabled,
        providers=providers,
        stages=stages,
        permissions=permissions,
        content_sha256=content_sha256,
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
