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
    CapabilityRegistryError,
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
) -> dict[str, object]:
    """Validate and preview a local capability copy without writing."""
    root = Path(project).resolve()
    files, source_name, runtime_installed, registered_digest = _validated_source(
        root, kind, Path(source)
    )
    destination = VENDOR_ROOT / capability_id
    relative_source = _registered_source(destination, kind)
    permissions = ["workspace-read", "subprocess"] if kind == "mcp" else ["workspace-read"]
    entry = validate_capability_declaration(
        root,
        capability_id=capability_id,
        kind=kind,
        source=relative_source.as_posix(),
        providers=providers,
        stages=stages,
        permissions=permissions,
    )
    if any(item.id == capability_id for item in load_registry(root)):
        raise CapabilityRegistryError(f"capability is already registered: {capability_id}")
    target = root / destination
    _validate_destination(root, target)
    rows = _file_rows(files)
    source_tree_sha256 = _tree_digest(rows)
    receipt = {
        "format_version": 1,
        "capability_id": capability_id,
        "kind": kind,
        "provenance": "operator-supplied-local-source",
        "source_name": source_name,
        "source_tree_sha256": source_tree_sha256,
        "files": rows,
    }
    return {
        "planned": True,
        "operation": "vendor-local-capability",
        "changed": True,
        "entry": {**entry, "content_sha256": registered_digest},
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
            "vendored": False,
            "registered": False,
            "enabled": False,
            "runtime_installed": runtime_installed,
            "available": False,
            "attached": False,
            "loaded": False,
            "invoked": False,
        },
        "post_apply": {
            "vendored": True,
            "registered": True,
            "enabled": False,
            "runtime_installed": runtime_installed,
            "attached": False,
            "loaded": False,
            "invoked": False,
        },
        "planned_writes": [destination.as_posix(), ".stagemesh/capabilities.json"],
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
    )
    target = root / str(plan["destination"])
    vendor_root = root / VENDOR_ROOT
    stage: Path | None = None
    lock_descriptor = -1
    lock_path: Path | None = None
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
        if target.exists() or target.is_symlink():
            raise CapabilityRegistryError("vendor destination appeared during staging; overwrite is refused")
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
                update=False,
            )
        except Exception:
            _remove_exact_target(root, target)
            raise
    except CapabilityRegistryError:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    except (OSError, ClaudePluginCapabilityError, McpCapabilityError) as exc:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
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
        "lifecycle": {
            "vendored": True,
            "registered": True,
            "enabled": False,
            "runtime_installed": registration["inventory"]["runtime_installed"],
            "available": registration["inventory"]["available"],
            "attached": False,
            "loaded": False,
            "invoked": False,
        },
        "inventory": registration["inventory"],
    }


def _validated_source(
    project: Path, kind: str, source: Path
) -> tuple[list[tuple[Path, bytes]], str, bool, str]:
    path = source.resolve()
    if source.is_symlink() or not path.exists():
        raise CapabilityRegistryError("vendor source must exist and must not be a symlink")
    if kind == "skill":
        if not path.is_file() or path.name != "SKILL.md":
            raise CapabilityRegistryError("skill vendor source must be one standalone SKILL.md file")
        data = _read_bounded(path, MAX_SINGLE_FILE_BYTES)
        digest = hashlib.sha256(data).hexdigest()
        return [(Path("SKILL.md"), data)], path.name, True, digest
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


def _validate_destination(root: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise CapabilityRegistryError("vendor destination already exists; overwrite is refused")
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
