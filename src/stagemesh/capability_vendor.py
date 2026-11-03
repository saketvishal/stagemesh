"""Bounded local vendoring for reviewed capability sources.

Vendoring copies an explicitly selected local source into the project.  It is
not a network/package-manager install and never enables, attaches, or invokes
the capability.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from .capabilities import (
    CapabilityDefinition,
    CapabilityRegistryError,
    inventory,
    load_registry,
    register_capability,
    validate_capability_declaration,
)
from .claude_plugin_capabilities import (
    ClaudePluginCapabilityError,
    load_claude_plugin,
)
from .mcp_capabilities import McpCapabilityError, load_mcp_manifest

VENDOR_ROOT = Path(".stagemesh/vendor/capabilities")
RECEIPT_NAME = ".stagemesh-vendor.json"
MAX_VENDOR_FILES = 128
MAX_VENDOR_BYTES = 2 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 256 * 1024


def plan_capability_vendor(
    project: Path,
    *,
    capability_id: str,
    kind: str,
    source: Path,
    providers: list[str],
    stages: list[str],
    update: bool = False,
) -> dict[str, object]:
    """Validate and preview a local capability copy without writing."""
    root = Path(project).resolve()
    files, source_name, runtime_installed, registered_digest = _validated_source(
        root, kind, Path(source)
    )
    destination = VENDOR_ROOT / capability_id
    relative_source = _registered_source(destination, kind)
    permissions = ["workspace-read", "subprocess"] if kind == "mcp" else ["workspace-read"]
    existing = next((item for item in load_registry(root) if item.id == capability_id), None)
    target = root / destination
    if update:
        if existing is None:
            raise CapabilityRegistryError(f"cannot refresh unregistered capability: {capability_id}")
        if (
            existing.kind != kind
            or existing.source != relative_source.as_posix()
            or existing.source_tree_sha256 is None
        ):
            raise CapabilityRegistryError(
                "refresh is limited to a matching capability created by the local vendor operation"
            )
    elif existing is not None:
        raise CapabilityRegistryError(f"capability is already registered: {capability_id}")
    _validate_destination(root, target, update=update)
    rows = _file_rows(files)
    source_tree_sha256 = _tree_digest(rows)
    entry = validate_capability_declaration(
        root,
        capability_id=capability_id,
        kind=kind,
        source=relative_source.as_posix(),
        providers=providers,
        stages=stages,
        permissions=permissions,
        source_tree_sha256=source_tree_sha256,
    )
    receipt = {
        "format_version": 1,
        "capability_id": capability_id,
        "kind": kind,
        "provenance": "operator-supplied-local-source",
        "source_name": source_name,
        "source_tree_sha256": source_tree_sha256,
        "files": rows,
    }
    next_entry = {**entry, "content_sha256": registered_digest}
    previous_entry = _definition_payload(existing) if existing is not None else None
    current_row = (
        next((row for row in inventory(root) if row["id"] == capability_id), None)
        if update
        else None
    )
    changed = previous_entry != next_entry or bool(
        update and (current_row is None or current_row["integrity"] != "verified")
    )
    predicted_available = runtime_installed and (
        kind in {"skill", "claude-plugin"} or "codex" in providers
    )
    return {
        "planned": True,
        "operation": "refresh-vendored-capability" if update else "vendor-local-capability",
        "changed": changed,
        "entry": next_entry,
        "previous_entry": previous_entry,
        "source": {
            "name": source_name,
            "file_count": len(rows),
            "bytes": sum(int(row["bytes"]) for row in rows),
            "tree_sha256": source_tree_sha256,
        },
        "destination": destination.as_posix(),
        "registered_source": relative_source.as_posix(),
        "receipt": receipt,
        "lifecycle": {
            "vendored": bool(update and target.is_dir()),
            "registered": update,
            "enabled": existing.enabled if existing is not None else False,
            "runtime_installed": (
                current_row["runtime_installed"]
                if current_row is not None
                else runtime_installed
            ),
            "available": current_row["available"] if current_row is not None else False,
            "attached": False,
            "activated": False,
            "loaded": False,
            "invoked": False,
        },
        "post_apply": {
            "vendored": True,
            "registered": True,
            "enabled": False,
            "runtime_installed": runtime_installed,
            "available": predicted_available,
            "attached": False,
            "activated": False,
            "loaded": False,
            "invoked": False,
        },
        "planned_writes": (
            [destination.as_posix(), ".stagemesh/capabilities.json"] if changed else []
        ),
        "writes": [],
        "provider_calls": 0,
        "network_calls": 0,
        "package_installs": 0,
        "database_writes": 0,
        "subprocess_calls": 0,
        "user_home_writes": 0,
    }


def vendor_capability(
    project: Path,
    *,
    capability_id: str,
    kind: str,
    source: Path,
    providers: list[str],
    stages: list[str],
    update: bool = False,
) -> dict[str, object]:
    """Stage a local copy and register it disabled, rolling back on refusal."""
    root = Path(project).resolve()
    plan = plan_capability_vendor(
        root,
        capability_id=capability_id,
        kind=kind,
        source=source,
        providers=providers,
        stages=stages,
        update=update,
    )
    if not plan["changed"]:
        current = next(row for row in inventory(root) if row["id"] == capability_id)
        return {
            **plan,
            "planned": False,
            "applied": True,
            "writes": [],
            "written": False,
            "lifecycle": _applied_lifecycle(current),
            "inventory": current,
        }
    target = root / str(plan["destination"])
    vendor_root = root / VENDOR_ROOT
    stage: Path | None = None
    lock_descriptor = -1
    lock_path: Path | None = None
    backup: Path | None = None
    try:
        _validate_vendor_root(root, vendor_root)
        vendor_root.mkdir(parents=True, exist_ok=True)
        _validate_vendor_root(root, vendor_root)
        lock_path = vendor_root / f".{capability_id}.vendor.lock"
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        stage = Path(tempfile.mkdtemp(dir=vendor_root, prefix=f".{capability_id}."))
        files, _name, _runtime, digest = _validated_source(root, kind, Path(source))
        rows = _file_rows(files)
        if (
            _tree_digest(rows) != plan["source"]["tree_sha256"]
            or digest != plan["entry"]["content_sha256"]
        ):
            raise CapabilityRegistryError("vendor source changed after validation; retry explicitly")
        for relative, data in files:
            output = stage / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        (stage / RECEIPT_NAME).write_text(
            json.dumps(plan["receipt"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="",
        )
        _validate_staged(root, stage, kind)
        if not update and (target.exists() or target.is_symlink()):
            raise CapabilityRegistryError("vendor destination appeared during staging; overwrite is refused")
        if update and target.exists():
            if target.is_symlink() or not target.is_dir():
                raise CapabilityRegistryError("vendored refresh target became unsafe")
            backup = Path(tempfile.mkdtemp(dir=vendor_root, prefix=f".{capability_id}.backup."))
            backup.rmdir()
            os.replace(target, backup)
        os.replace(stage, target)
        stage = None
        try:
            registration = register_capability(
                root,
                capability_id=capability_id,
                kind=kind,
                source=str(plan["registered_source"]),
                providers=providers,
                stages=stages,
                permissions=(
                    ["workspace-read", "subprocess"] if kind == "mcp" else ["workspace-read"]
                ),
                enabled=False,
                update=update,
                source_tree_sha256=str(plan["source"]["tree_sha256"]),
            )
        except Exception:
            _remove_exact_target(root, target)
            if backup is not None and backup.exists():
                os.replace(backup, target)
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
            backup = None
    except CapabilityRegistryError:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        _restore_backup(target, backup)
        raise
    except (OSError, ClaudePluginCapabilityError, McpCapabilityError) as exc:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        _restore_backup(target, backup)
        raise CapabilityRegistryError(f"cannot vendor capability: {exc}") from exc
    finally:
        if lock_descriptor >= 0:
            try:
                os.close(lock_descriptor)
            finally:
                if lock_path is not None:
                    try:
                        lock_path.unlink(missing_ok=True)
                    except OSError:
                        pass
    return {
        **plan,
        "planned": False,
        "applied": True,
        "writes": list(plan["planned_writes"]),
        "written": True,
        "lifecycle": _applied_lifecycle(registration["inventory"]),
        "inventory": registration["inventory"],
    }


def _validated_source(
    project: Path, kind: str, source: Path
) -> tuple[list[tuple[Path, bytes]], str, bool, str]:
    path = source.resolve()
    if source.is_symlink() or not path.exists():
        raise CapabilityRegistryError("vendor source must exist and must not be a symlink")
    if kind == "skill":
        if path.is_file() and path.name == "SKILL.md":
            data = _read_bounded(path, MAX_SINGLE_FILE_BYTES)
            return (
                [(Path("SKILL.md"), data)],
                path.name,
                True,
                hashlib.sha256(data).hexdigest(),
            )
        if path.is_dir() and (path / "SKILL.md").is_file():
            files = _skill_package_files(path)
            skill_data = next(data for relative, data in files if relative == Path("SKILL.md"))
            return files, path.name, True, hashlib.sha256(skill_data).hexdigest()
        raise CapabilityRegistryError(
            "skill vendor source must be a SKILL.md file or a directory containing SKILL.md"
        )
    if kind == "mcp":
        if not path.is_file():
            raise CapabilityRegistryError("MCP vendor source must be one manifest file")
        try:
            config = load_mcp_manifest(path, project)
        except McpCapabilityError as exc:
            raise CapabilityRegistryError(str(exc)) from exc
        data = _read_bounded(path, MAX_SINGLE_FILE_BYTES)
        return (
            [(Path("manifest.mcp.json"), data)],
            path.name,
            shutil.which(config.command) is not None,
            hashlib.sha256(data).hexdigest(),
        )
    if kind == "claude-plugin":
        if not path.is_dir():
            raise CapabilityRegistryError("Claude plugin vendor source must be a directory")
        try:
            plugin = load_claude_plugin(path, path.parent)
        except ClaudePluginCapabilityError as exc:
            raise CapabilityRegistryError(str(exc)) from exc
        files = _plugin_files(path)
        return files, path.name, True, plugin.content_sha256
    raise CapabilityRegistryError(f"unsupported capability kind: {kind}")


def _plugin_files(root: Path) -> list[tuple[Path, bytes]]:
    accepted: list[tuple[Path, bytes]] = []
    entries = 0
    total = 0
    for item in sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()):
        entries += 1
        if entries > MAX_VENDOR_FILES:
            raise CapabilityRegistryError(f"vendor source exceeds {MAX_VENDOR_FILES} entries")
        relative = item.relative_to(root)
        if item.is_symlink():
            raise CapabilityRegistryError(f"vendor source refuses symlink: {relative.as_posix()}")
        if item.is_dir():
            continue
        allowed = (
            relative == Path(".claude-plugin/plugin.json")
            or len(relative.parts) == 3
            and relative.parts[0] == "skills"
            and relative.parts[2] == "SKILL.md"
        )
        if not item.is_file() or not allowed:
            raise CapabilityRegistryError(
                f"skills-only Claude plugin vendor refuses unsupported file: {relative.as_posix()}"
            )
        data = _read_bounded(item, MAX_SINGLE_FILE_BYTES)
        total += len(data)
        if total > MAX_VENDOR_BYTES:
            raise CapabilityRegistryError(f"vendor source exceeds {MAX_VENDOR_BYTES} bytes")
        accepted.append((relative, data))
    return accepted


def _skill_package_files(root: Path) -> list[tuple[Path, bytes]]:
    accepted: list[tuple[Path, bytes]] = []
    entries = 0
    total = 0
    for item in sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()):
        entries += 1
        if entries > MAX_VENDOR_FILES:
            raise CapabilityRegistryError(f"vendor source exceeds {MAX_VENDOR_FILES} entries")
        relative = item.relative_to(root)
        if item.is_symlink():
            raise CapabilityRegistryError(f"vendor source refuses symlink: {relative.as_posix()}")
        if item.is_dir():
            continue
        if not item.is_file() or relative.as_posix() == RECEIPT_NAME:
            raise CapabilityRegistryError(
                f"skill package vendor refuses unsupported file: {relative.as_posix()}"
            )
        data = _read_bounded(item, MAX_SINGLE_FILE_BYTES)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CapabilityRegistryError(
                f"skill package files must be UTF-8 text: {relative.as_posix()}"
            ) from exc
        if "\x00" in text:
            raise CapabilityRegistryError(
                f"skill package files must not contain NUL bytes: {relative.as_posix()}"
            )
        total += len(data)
        if total > MAX_VENDOR_BYTES:
            raise CapabilityRegistryError(f"vendor source exceeds {MAX_VENDOR_BYTES} bytes")
        accepted.append((relative, data))
    return accepted


def _read_bounded(path: Path, maximum: int) -> bytes:
    try:
        size = path.stat().st_size
        if size > maximum:
            raise CapabilityRegistryError(f"vendor source file exceeds {maximum} bytes: {path.name}")
        return path.read_bytes()
    except OSError as exc:
        raise CapabilityRegistryError(f"cannot read vendor source: {exc}") from exc


def _file_rows(files: list[tuple[Path, bytes]]) -> list[dict[str, object]]:
    return [
        {
            "path": path.as_posix(),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for path, data in files
    ]


def _tree_digest(rows: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(str(row["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(row["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _registered_source(destination: Path, kind: str) -> Path:
    if kind == "skill":
        return destination / "SKILL.md"
    if kind == "mcp":
        return destination / "manifest.mcp.json"
    return destination


def _validate_destination(root: Path, target: Path, *, update: bool) -> None:
    if not update and (target.exists() or target.is_symlink()):
        raise CapabilityRegistryError("vendor destination already exists; overwrite is refused")
    if update and (target.is_symlink() or target.exists() and not target.is_dir()):
        raise CapabilityRegistryError("vendored refresh target is unsafe")
    _validate_vendor_root(root, root / VENDOR_ROOT)


def _validate_vendor_root(root: Path, vendor_root: Path) -> None:
    if not vendor_root.resolve().is_relative_to(root):
        raise CapabilityRegistryError("vendor destination must stay inside the project")
    current = root
    for part in VENDOR_ROOT.parts:
        current = current / part
        if current.is_symlink():
            raise CapabilityRegistryError("vendor destination refuses a symlink directory")


def _validate_staged(project: Path, stage: Path, kind: str) -> None:
    if kind == "skill":
        if not (stage / "SKILL.md").is_file():
            raise CapabilityRegistryError("staged skill is missing SKILL.md")
    elif kind == "mcp":
        load_mcp_manifest(stage / "manifest.mcp.json", project)
    else:
        load_claude_plugin(stage, project)


def _remove_exact_target(root: Path, target: Path) -> None:
    expected_parent = (root / VENDOR_ROOT).resolve()
    if target.parent.resolve() != expected_parent or not target.resolve().is_relative_to(root):
        raise CapabilityRegistryError("refusing unsafe vendor rollback target")
    shutil.rmtree(target)


def _restore_backup(target: Path, backup: Path | None) -> None:
    if backup is None or not backup.exists():
        return
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    os.replace(backup, target)


def _definition_payload(item: CapabilityDefinition | None) -> dict[str, object] | None:
    if item is None:
        return None
    payload: dict[str, object] = {
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
    if item.source_tree_sha256 is not None:
        payload["source_tree_sha256"] = item.source_tree_sha256
    return payload


def _applied_lifecycle(row: dict[str, object]) -> dict[str, object]:
    return {
        "vendored": True,
        "registered": True,
        "enabled": False,
        "runtime_installed": row["runtime_installed"],
        "available": row["available"],
        "attached": False,
        "activated": False,
        "loaded": False,
        "invoked": False,
    }
