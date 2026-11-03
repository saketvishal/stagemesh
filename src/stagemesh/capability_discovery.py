"""Bounded, read-only discovery of project-local capability candidates."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .capabilities import (
    CapabilityRegistryError,
    inventory,
    load_registry,
    plan_capability_registration,
)
from .claude_plugin_capabilities import (
    ClaudePluginCapabilityError,
    load_claude_plugin,
)
from .mcp_capabilities import McpCapabilityError, load_mcp_manifest

MAX_DISCOVERY_ENTRIES = 2048
MAX_DISCOVERY_CANDIDATES = 128
MAX_DISCOVERY_DEPTH = 12
MAX_SKILL_BYTES = 256 * 1024
_PORTABLE = re.compile(r"[^a-z0-9._-]+")


def discover_capabilities(
    project: Path,
    search_root: str,
    *,
    max_entries: int = MAX_DISCOVERY_ENTRIES,
    max_candidates: int = MAX_DISCOVERY_CANDIDATES,
    max_depth: int = MAX_DISCOVERY_DEPTH,
) -> dict[str, object]:
    """Inspect one explicit project subtree without following links or mutating state."""
    project_root = Path(project).resolve()
    relative_root = _relative_root(search_root)
    root = project_root / relative_root
    cursor = project_root
    for part in relative_root.parts:
        cursor /= part
        if cursor.is_symlink():
            raise CapabilityRegistryError("capability discovery root must not contain a symlink")
    resolved_root = root.resolve()
    if not resolved_root.is_relative_to(project_root) or not resolved_root.is_dir():
        raise CapabilityRegistryError(
            "capability discovery root must be an existing project-relative directory"
        )
    files = _bounded_files(
        resolved_root,
        max_entries=max_entries,
        max_depth=max_depth,
    )
    definitions = load_registry(project_root)
    registered_by_source = {item.source: item.id for item in definitions}
    registered_ids = {item.id for item in definitions}
    inventory_by_source = {str(row["source"]): row for row in inventory(project_root)}

    plugin_roots = sorted(
        {
            *(
                path.parent.parent
                for path in files
                if path.name == "plugin.json" and path.parent.name == ".claude-plugin"
                and path.parent.parent.is_relative_to(resolved_root)
            ),
            *(
                path.parent.parent.parent
                for path in files
                if path.name == "SKILL.md" and path.parent.parent.name == "skills"
                and path.parent.parent.parent.is_relative_to(resolved_root)
            ),
        },
        key=lambda item: item.as_posix(),
    )
    plugin_skill_paths: set[Path] = set()
    candidates: list[dict[str, object]] = []
    for plugin_root in plugin_roots:
        for path in files:
            if path.name == "SKILL.md" and path.is_relative_to(plugin_root / "skills"):
                plugin_skill_paths.add(path)
        try:
            plugin = load_claude_plugin(plugin_root, project_root)
            candidates.append(
                _candidate(
                    project_root,
                    candidate_id=plugin.name,
                    kind="claude-plugin",
                    source=plugin_root,
                    registered_by_source=registered_by_source,
                    registered_ids=registered_ids,
                    inventory_by_source=inventory_by_source,
                    valid=True,
                    runtime_installed=True,
                )
            )
        except ClaudePluginCapabilityError as exc:
            candidates.append(
                _candidate(
                    project_root,
                    candidate_id=_portable_id(plugin_root.name),
                    kind="claude-plugin",
                    source=plugin_root,
                    registered_by_source=registered_by_source,
                    registered_ids=registered_ids,
                    inventory_by_source=inventory_by_source,
                    valid=False,
                    reason=str(exc),
                    runtime_installed=False,
                )
            )

    for path in sorted(files, key=lambda item: item.as_posix()):
        if path.name == "SKILL.md" and path not in plugin_skill_paths:
            size = path.stat().st_size
            candidates.append(
                _candidate(
                    project_root,
                    candidate_id=_portable_id(path.parent.name),
                    kind="skill",
                    source=path,
                    registered_by_source=registered_by_source,
                    registered_ids=registered_ids,
                    inventory_by_source=inventory_by_source,
                    valid=size <= MAX_SKILL_BYTES,
                    reason=(
                        None
                        if size <= MAX_SKILL_BYTES
                        else f"SKILL.md exceeds the {MAX_SKILL_BYTES}-byte discovery limit"
                    ),
                    runtime_installed=True,
                )
            )
        elif _looks_like_mcp_manifest(path, resolved_root):
            try:
                manifest = load_mcp_manifest(path, project_root)
                candidates.append(
                    _candidate(
                        project_root,
                        candidate_id=manifest.server_name,
                        kind="mcp",
                        source=path,
                        registered_by_source=registered_by_source,
                        registered_ids=registered_ids,
                        inventory_by_source=inventory_by_source,
                        valid=True,
                        runtime_installed=shutil.which(manifest.command) is not None,
                    )
                )
            except McpCapabilityError as exc:
                candidates.append(
                    _candidate(
                        project_root,
                        candidate_id=_portable_id(path.stem.removesuffix(".mcp")),
                        kind="mcp",
                        source=path,
                        registered_by_source=registered_by_source,
                        registered_ids=registered_ids,
                        inventory_by_source=inventory_by_source,
                        valid=False,
                        reason=str(exc),
                        runtime_installed=False,
                    )
                )
        if len(candidates) > max_candidates:
            raise CapabilityRegistryError(
                f"capability discovery found more than {max_candidates} candidates"
            )

    candidates.sort(key=lambda item: (str(item["source"]), str(item["kind"])))
    collisions: dict[str, list[str]] = {}
    for row in candidates:
        collisions.setdefault(str(row["id"]), []).append(str(row["source"]))
    duplicates = {key: value for key, value in collisions.items() if len(value) > 1}
    if duplicates:
        details = "; ".join(f"{key}: {', '.join(value)}" for key, value in sorted(duplicates.items()))
        raise CapabilityRegistryError(f"capability discovery candidate id collision: {details}")
    return {
        "project": str(project_root),
        "root": relative_root.as_posix(),
        "read_only": True,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "limits": {
            "entries": max_entries,
            "candidates": max_candidates,
            "depth": max_depth,
            "skill_bytes": MAX_SKILL_BYTES,
        },
        "writes": [],
        "provider_calls": 0,
        "network_calls": 0,
        "package_installs": 0,
        "database_writes": 0,
        "subprocess_calls": 0,
    }


def plan_discovered_registration(
    project: Path,
    discovery: dict[str, object],
    candidate_id: str,
    *,
    providers: list[str],
    stages: list[str],
) -> dict[str, object]:
    """Reuse the production registration planner for one discovered candidate."""
    rows = discovery.get("candidates")
    if not isinstance(rows, list):
        raise CapabilityRegistryError("invalid capability discovery result")
    matches = [row for row in rows if isinstance(row, dict) and row.get("id") == candidate_id]
    if len(matches) != 1:
        raise CapabilityRegistryError(
            f"discovered capability candidate must match exactly once: {candidate_id}"
        )
    row = matches[0]
    if not row.get("valid"):
        raise CapabilityRegistryError(
            f"discovered capability candidate is invalid: {row.get('reason') or candidate_id}"
        )
    if row.get("registered"):
        raise CapabilityRegistryError(
            f"discovered capability source is already registered: {row.get('registered_id')}"
        )
    kind = str(row["kind"])
    permissions = ["workspace-read", "subprocess"] if kind == "mcp" else ["workspace-read"]
    plan = plan_capability_registration(
        project,
        capability_id=candidate_id,
        kind=kind,
        source=str(row["source"]),
        providers=providers,
        stages=stages,
        permissions=permissions,
        enabled=False,
        update=False,
    )
    return {**plan, "discovered": row, "discovery_root": discovery["root"]}


def _bounded_files(root: Path, *, max_entries: int, max_depth: int) -> list[Path]:
    files: list[Path] = []
    entries = 0
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > max_depth:
            raise CapabilityRegistryError(
                f"capability discovery exceeds the maximum depth of {max_depth}"
            )
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise CapabilityRegistryError(f"cannot read capability discovery root: {exc}") from exc
        for child in children:
            entries += 1
            if entries > max_entries:
                raise CapabilityRegistryError(
                    f"capability discovery exceeds the {max_entries}-entry limit"
                )
            if child.is_symlink():
                raise CapabilityRegistryError(
                    f"capability discovery refuses symlink: {child.relative_to(root).as_posix()}"
                )
            if child.is_dir():
                stack.append((child, depth + 1))
            elif child.is_file():
                files.append(child)
    return files


def _candidate(
    project: Path,
    *,
    candidate_id: str,
    kind: str,
    source: Path,
    registered_by_source: dict[str, str],
    registered_ids: set[str],
    inventory_by_source: dict[str, dict[str, object]],
    valid: bool,
    runtime_installed: bool,
    reason: str | None = None,
) -> dict[str, object]:
    relative = source.relative_to(project).as_posix()
    registered_id = registered_by_source.get(relative)
    current = inventory_by_source.get(relative, {})
    return {
        "id": candidate_id,
        "kind": kind,
        "source": relative,
        "discovered": True,
        "valid": valid,
        "reason": None if reason is None else reason[:300],
        "registered": registered_id is not None,
        "registered_id": registered_id,
        "id_registered": candidate_id in registered_ids,
        "installed": True,
        "adapter_supported": True,
        "configuration_valid": valid,
        "enabled": current.get("enabled", False),
        "available": current.get("available", False),
        "usable": current.get("usable", False),
        "runtime_installed": runtime_installed,
        "invocation": "not-observed",
    }


def _relative_root(value: str) -> Path:
    relative = Path(str(value).strip())
    if (
        not str(value).strip()
        or relative.is_absolute()
        or relative.drive
        or ".." in relative.parts
    ):
        raise CapabilityRegistryError(
            "capability discovery root must be a project-relative path without parent traversal"
        )
    return relative


def _portable_id(value: str) -> str:
    normalized = _PORTABLE.sub("-", value.casefold()).strip("._-")[:100]
    if not normalized:
        raise CapabilityRegistryError("capability discovery cannot derive a portable candidate id")
    return normalized


def _looks_like_mcp_manifest(path: Path, discovery_root: Path) -> bool:
    if path.suffix.casefold() != ".json":
        return False
    return (
        path.name.casefold().endswith(".mcp.json")
        or discovery_root.name.casefold() == "mcp"
        or any(
            part.casefold() == "mcp"
            for part in path.relative_to(discovery_root).parts[:-1]
        )
    )
