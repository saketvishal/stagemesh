"""Safe task-contract capability selection and provider routing previews."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .capabilities import (
    CapabilityRegistryError,
    inventory,
    load_registry,
    requested_for_stage,
    resolve_attachments,
    skill_evidence_mode,
)
from .capacity import CapacityKind
from .config import StageMeshConfig
from .contracts import ChangeContract, ContractError, parse_contract
from .mcp_capabilities import McpCapabilityError, configure_codex_mcp_command
from .provider_events import ProviderEventError, event_stream_command
from .provider_pool import IMPLEMENT, REVIEW, default_pools
from .providers import adapters_from_config

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SUPPORTED_CONTRACT_FIELDS = frozenset(
    {
        "objective",
        "explicit",
        "validation_classification",
        "validation_risk",
        "validation_escalation_reasons",
        "acceptance_criteria",
        "spec_refs",
        "skill_refs",
        "capability_refs",
        "allowed_files",
        "forbidden_files",
        "exclusions",
        "invariants",
        "required_tests",
        "lint",
        "typecheck",
        "dependency_checks",
        "public_api",
        "protected_files",
        "exclusive_resources",
        "max_changed_files",
        "max_diff_lines",
    }
)


def plan_task_capabilities(
    project: Path,
    task_id: str,
    *,
    add: list[str] | tuple[str, ...] = (),
    remove: list[str] | tuple[str, ...] = (),
    database: Path | None = None,
    config: StageMeshConfig | None = None,
) -> dict[str, object]:
    """Plan one task contract's capability_refs change without writing state."""
    plan, _payload = _task_capability_change(
        project,
        task_id,
        add=add,
        remove=remove,
        database=database,
        config=config,
    )
    return plan


def _task_capability_change(
    project: Path,
    task_id: str,
    *,
    add: list[str] | tuple[str, ...] = (),
    remove: list[str] | tuple[str, ...] = (),
    database: Path | None = None,
    config: StageMeshConfig | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    root, path, raw, contract = _load_task_contract(project, task_id)
    additions = _ids(add, "add")
    removals = _ids(remove, "remove")
    overlap = sorted(set(additions).intersection(removals))
    if overlap:
        raise CapabilityRegistryError(
            "capability ids cannot be added and removed together: " + ", ".join(overlap)
        )
    if not additions and not removals:
        raise CapabilityRegistryError("task capability change requires --add or --remove")

    rows = {str(row["id"]): row for row in inventory(root)}
    definitions = {item.id: item for item in load_registry(root)}
    for capability_id in additions:
        row = rows.get(capability_id)
        if row is None:
            raise CapabilityRegistryError(
                f"cannot add unregistered capability to task contract: {capability_id}"
            )
        if not row["enabled"]:
            raise CapabilityRegistryError(
                f"cannot add disabled capability to task contract: {capability_id}"
            )
        if not row["available"]:
            raise CapabilityRegistryError(
                f"cannot add unavailable capability to task contract: {capability_id}"
            )
        if not definitions[capability_id].stages:
            raise CapabilityRegistryError(
                f"cannot add capability with no lifecycle stage: {capability_id}"
            )

    current = list(contract.capability_refs)
    proposed = [item for item in current if item not in set(removals)]
    proposed.extend(item for item in additions if item not in proposed)
    changed = proposed != current
    next_raw = dict(raw)
    if proposed:
        next_raw["capability_refs"] = proposed
    else:
        next_raw.pop("capability_refs", None)
    try:
        proposed_contract = parse_contract(next_raw)
    except ContractError as exc:
        raise CapabilityRegistryError(f"proposed task contract is invalid: {exc}") from exc

    routing = capability_preflight(root, proposed_contract, config) if config is not None else None
    if routing is not None:
        routed = {
            str(stage["stage"]): set(stage["pool"])
            for stage in routing["stages"]
            if isinstance(stage, dict)
        }
        for capability_id in additions:
            item = definitions[capability_id]
            compatible = any(
                set(item.providers).intersection(routed.get(stage, set()))
                for stage in item.stages
            )
            if not compatible:
                raise CapabilityRegistryError(
                    f"capability {capability_id} has no compatible configured provider route"
                )
    frozen = _has_frozen_contract(database, task_id)
    relative = path.relative_to(root).as_posix()
    selected = [rows[item] for item in proposed if item in rows]
    plan = {
        "planned": True,
        "operation": "set-task-capabilities",
        "task_id": task_id,
        "contract": relative,
        "current_capability_refs": current,
        "proposed_capability_refs": proposed,
        "added": [item for item in proposed if item not in current],
        "removed": [item for item in current if item not in proposed],
        "changed": changed,
        "selected": selected,
        "routing": routing,
        "runtime_contract_frozen": frozen,
        "rebind_required": bool(changed and frozen),
        "next_command": (
            f"stagemesh rebind-contract --task {task_id} --plan"
            if changed and frozen
            else None
        ),
        "planned_writes": [relative] if changed else [],
        "writes": [],
        "database_writes": 0,
        "provider_calls": 0,
        "network_calls": 0,
        "package_installs": 0,
    }
    return plan, next_raw


def set_task_capabilities(
    project: Path,
    task_id: str,
    *,
    add: list[str] | tuple[str, ...] = (),
    remove: list[str] | tuple[str, ...] = (),
    database: Path | None = None,
    config: StageMeshConfig | None = None,
) -> dict[str, object]:
    """Apply an already validated task capability selection atomically."""
    plan, payload = _task_capability_change(
        project,
        task_id,
        add=add,
        remove=remove,
        database=database,
        config=config,
    )
    if plan["changed"]:
        root = Path(project).resolve()
        path = root / str(plan["contract"])
        _write_contract(root, path, payload)
    return {
        **plan,
        "planned": False,
        "applied": True,
        "written": bool(plan["changed"]),
        "writes": list(plan["planned_writes"]) if plan["changed"] else [],
    }


def task_capability_preflight(
    project: Path, task_id: str, config: StageMeshConfig
) -> dict[str, object]:
    """Read one task contract and preview exact capability/provider routing."""
    root, path, _raw, contract = _load_task_contract(project, task_id)
    return {
        "task_id": task_id,
        "contract": path.relative_to(root).as_posix(),
        **capability_preflight(root, contract, config),
    }


def capability_preflight(
    project: Path, contract: ChangeContract, config: StageMeshConfig
) -> dict[str, object]:
    """Apply production capability resolution to every configured stage route."""
    root = Path(project).resolve()
    adapters = adapters_from_config(config)
    adapter_by_name = {adapter.name: adapter for adapter in adapters}
    stage_capability = {IMPLEMENT: "code", REVIEW: "review"}
    capable = {
        stage: {
            adapter.name
            for adapter in adapters
            if capability in adapter.capabilities
        }
        for stage, capability in stage_capability.items()
    }
    priorities = {
        name: spec.priority
        for name, spec in config.provider_specs.items()
        if spec.priority is not None
    }
    pools = default_pools(
        sorted(adapter_by_name),
        config.stage_routes,
        config.provider_pools,
        config.single_agent_provider,
        config.routing_mode,
        capable=capable,
        priorities=priorities,
    )
    stages: list[dict[str, object]] = []
    eligible_by_stage: dict[str, set[str]] = {}
    evidence_mode = skill_evidence_mode(root)
    definitions = {item.id: item for item in load_registry(root)}
    inventory_rows = {str(row["id"]): row for row in inventory(root)}
    for stage in (IMPLEMENT, REVIEW):
        selected = requested_for_stage(root, contract.capability_refs, stage=stage)
        selected_kinds = {definitions[item].kind for item in selected}
        evidence_requirements: list[str] = []
        if selected_kinds.intersection({"skill", "claude-plugin"}):
            evidence_requirements.append(
                "observed-load"
                if evidence_mode == "observed-load-required"
                else "attachment"
            )
        if "mcp" in selected_kinds:
            evidence_requirements.append("mcp-invocation")
        provider_rows: list[dict[str, object]] = []
        eligible: set[str] = set()
        for provider in pools.get(stage, ()):
            adapter = adapter_by_name.get(provider)
            reason = None
            if adapter is None:
                reason = "not_configured"
            elif adapter.denied:
                reason = f"not_authorized: {adapter.denied}"
            elif stage_capability[stage] not in adapter.capabilities:
                reason = f"missing_stage_capability: {stage_capability[stage]}"
            elif adapter.check_stage_capacity(stage_capability[stage]) != CapacityKind.AVAILABLE:
                reason = "provider_command_unavailable"
            else:
                try:
                    attachments = resolve_attachments(
                        root, selected, provider=provider, stage=stage
                    )
                    mcp_requested = any(item.kind == "mcp" for item in attachments)
                    observed_skill_requested = evidence_mode == "observed-load-required" and any(
                        item.kind in {"skill", "claude-plugin"} for item in attachments
                    )
                    command = adapter.command_for(stage_capability[stage])
                    if mcp_requested:
                        command, _permitted = configure_codex_mcp_command(
                            command, [item.audit_payload() for item in attachments]
                        )
                    if mcp_requested or observed_skill_requested:
                        event_stream_command(command)
                except (CapabilityRegistryError, McpCapabilityError, ProviderEventError) as exc:
                    reason = str(exc)
            if reason is None:
                eligible.add(provider)
            provider_rows.append(
                {
                    "provider": provider,
                    "eligible": reason is None,
                    "reason": reason,
                }
            )
        eligible_by_stage[stage] = eligible
        stages.append(
            {
                "stage": stage,
                "requested_capabilities": list(selected),
                "evidence_requirements": evidence_requirements,
                "pool": list(pools.get(stage, ())),
                "eligible_providers": sorted(eligible),
                "providers": provider_rows,
                "ready": bool(eligible),
            }
        )
    implementation = eligible_by_stage[IMPLEMENT]
    review = eligible_by_stage[REVIEW]
    independent_pair = any(i != r for i in implementation for r in review)
    ready = bool(implementation and review) and (
        independent_pair or not config.require_independent_review
    )
    any_route = bool(implementation or review)
    capability_rows: list[dict[str, object]] = []
    for capability_id in dict.fromkeys(contract.capability_refs):
        item = definitions[capability_id]
        row = inventory_rows[capability_id]
        relevant = [
            stage
            for stage in stages
            if capability_id in stage["requested_capabilities"]
        ]
        eligible_routes = sorted(
            {
                (str(stage["stage"]), str(provider))
                for stage in relevant
                for provider in stage["eligible_providers"]
            }
        )
        ineligible_routes = [
            {
                "stage": stage["stage"],
                "provider": provider["provider"],
                "reason": provider["reason"],
            }
            for stage in relevant
            for provider in stage["providers"]
            if isinstance(provider, dict) and not provider["eligible"]
        ]
        capability_rows.append(
            {
                "id": capability_id,
                "kind": item.kind,
                "registered": True,
                "installed": row["installed"],
                "available": row["available"],
                "enabled": row["enabled"],
                "usable": row["usable"],
                "requested_stages": list(item.stages),
                "declared_providers": list(item.providers),
                "eligible_routes": [
                    {"stage": stage, "provider": provider}
                    for stage, provider in eligible_routes
                ],
                "ineligible_routes": ineligible_routes,
                "evidence_requirements": sorted(
                    {
                        str(requirement)
                        for stage in relevant
                        for requirement in stage["evidence_requirements"]
                    }
                ),
            }
        )
    return {
        "status": "ready" if ready else "partial" if any_route else "blocked",
        "ready": ready,
        "requested_capabilities": list(contract.capability_refs),
        "capabilities": capability_rows,
        "skill_evidence_mode": evidence_mode,
        "independent_review_required": config.require_independent_review,
        "independent_provider_pair": independent_pair,
        "stages": stages,
        "read_only": True,
        "writes": [],
        "database_writes": 0,
        "provider_calls": 0,
        "network_calls": 0,
        "package_installs": 0,
    }


def _load_task_contract(
    project: Path, task_id: str
) -> tuple[Path, Path, dict[str, Any], ChangeContract]:
    root = Path(project).resolve()
    normalized = str(task_id).strip()
    if not _TASK_ID.fullmatch(normalized):
        raise CapabilityRegistryError("task id is not safe for a project contract path")
    candidates = (
        root / ".stagemesh" / "contracts" / f"{normalized}.json",
        root / ".stagemesh" / "contracts" / f"{normalized}.contract.json",
    )
    for candidate in candidates:
        if candidate.is_symlink():
            raise CapabilityRegistryError("task contract operation refuses a symlink path")
    path = next((candidate for candidate in candidates if candidate.exists()), None)
    if path is None:
        raise CapabilityRegistryError(
            f"no task contract found at .stagemesh/contracts/{normalized}.json"
        )
    _refuse_symlink_path(root, path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CapabilityRegistryError(f"cannot load task contract: {exc}") from exc
    if not isinstance(raw, dict):
        raise CapabilityRegistryError("task contract root must be an object")
    unsupported = sorted(set(raw) - _SUPPORTED_CONTRACT_FIELDS)
    if unsupported:
        raise CapabilityRegistryError(
            "task contract contains unsupported fields: " + ", ".join(unsupported)
        )
    try:
        contract = parse_contract(raw)
    except ContractError as exc:
        raise CapabilityRegistryError(f"task contract is invalid: {exc}") from exc
    return root, path, raw, contract


def _ids(values: list[str] | tuple[str, ...], operation: str) -> list[str]:
    result: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if not value:
            raise CapabilityRegistryError(f"capability {operation} id must be non-empty")
        if value in result:
            raise CapabilityRegistryError(
                f"duplicate capability {operation} id: {value}"
            )
        result.append(value)
    return result


def _has_frozen_contract(database: Path | None, task_id: str) -> bool:
    if database is None or not Path(database).is_file():
        return False
    path = Path(database).resolve()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only = ON")
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_contracts'"
        ).fetchone()
        if exists is None:
            return False
        return connection.execute(
            "SELECT 1 FROM task_contracts WHERE task_id=?", (task_id,)
        ).fetchone() is not None
    except sqlite3.DatabaseError as exc:
        raise CapabilityRegistryError(f"cannot inspect frozen task contract: {exc}") from exc
    finally:
        connection.close()


def _refuse_symlink_path(root: Path, path: Path) -> None:
    current = path
    while current != root:
        if current.is_symlink():
            raise CapabilityRegistryError("task contract operation refuses a symlink path")
        current = current.parent
    if not path.resolve().is_relative_to(root):
        raise CapabilityRegistryError("task contract must stay inside the project")


def _write_contract(root: Path, path: Path, payload: dict[str, object]) -> None:
    _refuse_symlink_path(root, path)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(name)
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
        raise CapabilityRegistryError(f"cannot write task contract: {exc}") from exc
