"""Validate and activate skills-only Claude Code plugins.

StageMesh activates only a project-local plugin tree containing bounded
``skills/*/SKILL.md`` files. Runtime components and skill metadata that could
expand provider authority remain unsupported.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_PLUGIN_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_SAFE_MANIFEST_FIELDS = frozenset(
    {
        "name",
        "displayName",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "metadata",
        "defaultEnabled",
    }
)
_RUNTIME_COMPONENTS = (
    "agents",
    "commands",
    "hooks",
    "monitors",
    "output-styles",
    "themes",
    "workflows",
    "bin",
    "scripts",
    "settings.json",
    ".mcp.json",
    ".lsp.json",
)
_MAX_SKILLS = 64
_MAX_SKILL_BYTES = 256 * 1024
_MAX_PLUGIN_SKILL_BYTES = 2 * 1024 * 1024
_MAX_TREE_ENTRIES = 256
_VENDOR_RECEIPT = ".stagemesh-vendor.json"
_FORBIDDEN_SKILL_METADATA = re.compile(r"(?im)^\s*(?:allowed-tools|hooks)\s*:")


class ClaudePluginCapabilityError(RuntimeError):
    """A Claude plugin cannot be activated through the skills-only adapter."""


@dataclass(frozen=True)
class ClaudePluginSkill:
    name: str
    path: Path
    content_sha256: str

    def private_payload(self) -> dict[str, str]:
        return {
            "name": self.name,
            "path": str(self.path.resolve()),
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class ClaudePluginCapability:
    name: str
    root: Path
    skills: tuple[ClaudePluginSkill, ...]
    content_sha256: str

    def private_payload(self) -> dict[str, object]:
        return {
            "plugin_name": self.name,
            "activation": "skills-only-plugin-dir",
            "skills": [skill.private_payload() for skill in self.skills],
        }


def load_claude_plugin(path: Path, project: Path) -> ClaudePluginCapability:
    """Validate a project-local skills-only plugin without executing its runtime."""
    root = Path(path).resolve()
    project_root = Path(project).resolve()
    if not root.is_dir() or not _inside(root, project_root):
        raise ClaudePluginCapabilityError("Claude plugin source must be a directory inside the project")
    entries = _tree_entries(root)

    manifest_path = root / ".claude-plugin" / "plugin.json"
    if manifest_path.exists() and (
        not manifest_path.is_file() or not _inside(manifest_path.resolve(), root)
    ):
        raise ClaudePluginCapabilityError("Claude plugin manifest must stay inside the plugin root")
    manifest = _manifest(manifest_path) if manifest_path.exists() else {}
    plugin_name = manifest.get("name", root.name)
    if not isinstance(plugin_name, str) or not _PLUGIN_NAME.fullmatch(plugin_name):
        raise ClaudePluginCapabilityError("Claude plugin name must be a lowercase kebab-case identifier")

    for relative in _RUNTIME_COMPONENTS:
        if (root / relative).exists():
            raise ClaudePluginCapabilityError(
                f"Claude plugin runtime component is unsupported by the skills-only adapter: {relative}"
            )
    metadata_dir = root / ".claude-plugin"
    if metadata_dir.exists():
        if not metadata_dir.is_dir() or not _inside(metadata_dir.resolve(), root):
            raise ClaudePluginCapabilityError("Claude plugin metadata must stay inside the plugin root")
        extras = [item.name for item in metadata_dir.iterdir() if item.name != "plugin.json"]
        if extras:
            raise ClaudePluginCapabilityError(
                "Claude plugin metadata directory may contain only plugin.json"
            )

    skills_dir = root / "skills"
    if not skills_dir.is_dir() or not _inside(skills_dir.resolve(), root):
        raise ClaudePluginCapabilityError("skills-only Claude plugin must contain a skills directory")
    skills: list[ClaudePluginSkill] = []
    total_bytes = 0
    for skill_dir in sorted((item for item in skills_dir.iterdir() if item.is_dir()), key=lambda item: item.name):
        name = skill_dir.name
        if not _SKILL_NAME.fullmatch(name):
            raise ClaudePluginCapabilityError(
                f"Claude plugin skill name must be a lowercase portable identifier: {name}"
            )
        skill_path = (skill_dir / "SKILL.md").resolve()
        if not skill_path.is_file() or not _inside(skill_path, root):
            raise ClaudePluginCapabilityError(
                f"Claude plugin skill must contain a project-local SKILL.md: {name}"
            )
        size = skill_path.stat().st_size
        if size > _MAX_SKILL_BYTES:
            raise ClaudePluginCapabilityError(f"Claude plugin skill is too large: {name}")
        total_bytes += size
        if total_bytes > _MAX_PLUGIN_SKILL_BYTES:
            raise ClaudePluginCapabilityError("Claude plugin skill content exceeds the bounded total size")
        data = skill_path.read_bytes()
        _validate_skill_document(data, name)
        skills.append(ClaudePluginSkill(name, skill_path, hashlib.sha256(data).hexdigest()))
    if not skills:
        raise ClaudePluginCapabilityError("skills-only Claude plugin must contain at least one SKILL.md")
    if len(skills) > _MAX_SKILLS:
        raise ClaudePluginCapabilityError(f"Claude plugin may expose at most {_MAX_SKILLS} skills")

    allowed_files = {skill.path for skill in skills}
    if manifest_path.exists():
        allowed_files.add(manifest_path.resolve())
    receipt_path = root / _VENDOR_RECEIPT
    if receipt_path.exists():
        _validate_vendor_receipt(receipt_path, root, allowed_files)
        allowed_files.add(receipt_path.resolve())
    allowed_directories = {
        root,
        root / ".claude-plugin",
        root / "skills",
        *(skill.path.parent for skill in skills),
    }
    for entry in entries:
        if entry.is_dir() and entry not in allowed_directories:
            raise ClaudePluginCapabilityError(
                f"skills-only Claude plugin contains an unsupported directory: {entry.relative_to(root).as_posix()}"
            )
        if entry.is_file() and entry not in allowed_files:
            raise ClaudePluginCapabilityError(
                f"skills-only Claude plugin contains an unsupported file: {entry.relative_to(root).as_posix()}"
            )

    aggregate = hashlib.sha256()
    for skill in skills:
        aggregate.update(skill.name.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(skill.content_sha256.encode("ascii"))
        aggregate.update(b"\n")
    return ClaudePluginCapability(plugin_name, root, tuple(skills), aggregate.hexdigest())


def configure_claude_plugin_command(
    command: Iterable[str], attachments: Iterable[dict[str, object]]
) -> list[str]:
    """Activate validated skills-only plugins for one Claude invocation."""
    args = list(command)
    selected = [item for item in attachments if item.get("kind") == "claude-plugin"]
    if not selected:
        return args
    executable = Path(args[0]).stem.casefold() if args else ""
    if executable != "claude":
        raise ClaudePluginCapabilityError(
            "Claude plugin runtime adapter requires the claude executable"
        )
    if any(
        token in {"--plugin-dir", "--plugin-url", "--bare", "--safe-mode"}
        or token.startswith(("--plugin-dir=", "--plugin-url="))
        for token in args
    ):
        raise ClaudePluginCapabilityError(
            "Claude plugin runtime adapter refuses conflicting provider plugin or safe-mode flags"
        )
    roots: set[Path] = set()
    for attachment in selected:
        runtime = attachment.get("_plugin")
        path = attachment.get("_path")
        project = attachment.get("_project")
        if not isinstance(runtime, dict) or runtime.get("activation") != "skills-only-plugin-dir":
            raise ClaudePluginCapabilityError(
                "selected Claude plugin is missing validated skills-only metadata"
            )
        if not isinstance(path, str) or not path:
            raise ClaudePluginCapabilityError(
                "selected Claude plugin is missing its project-local runtime path"
            )
        if not isinstance(project, str) or not project:
            raise ClaudePluginCapabilityError(
                "selected Claude plugin is missing its project isolation root"
            )
        root = Path(path).resolve()
        if root in roots:
            raise ClaudePluginCapabilityError(f"duplicate Claude plugin path: {root.name}")
        current = load_claude_plugin(root, Path(project))
        expected_digest = attachment.get("content_sha256")
        if not isinstance(expected_digest, str) or current.content_sha256 != expected_digest:
            raise ClaudePluginCapabilityError(
                f"selected Claude plugin changed after validation: {current.name}"
            )
        if current.private_payload() != runtime:
            raise ClaudePluginCapabilityError(
                f"selected Claude plugin metadata changed after validation: {current.name}"
            )
        roots.add(root)
        args.extend(("--plugin-dir", str(root)))
    return args


def _tree_entries(root: Path) -> tuple[Path, ...]:
    entries: list[Path] = []
    pending = [root]
    while pending:
        parent = pending.pop()
        for entry in parent.iterdir():
            if entry.is_symlink():
                raise ClaudePluginCapabilityError(
                    "skills-only Claude plugin must not contain symlinks"
                )
            resolved = entry.resolve()
            if not _inside(resolved, root):
                raise ClaudePluginCapabilityError(
                    "skills-only Claude plugin entry must stay inside the plugin root"
                )
            entries.append(resolved)
            if len(entries) > _MAX_TREE_ENTRIES:
                raise ClaudePluginCapabilityError(
                    f"skills-only Claude plugin may contain at most {_MAX_TREE_ENTRIES} entries"
                )
            if entry.is_dir():
                pending.append(entry)
            elif not entry.is_file():
                raise ClaudePluginCapabilityError(
                    "skills-only Claude plugin entries must be regular files or directories"
                )
    return tuple(entries)


def _validate_skill_document(data: bytes, name: str) -> None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ClaudePluginCapabilityError(
            f"Claude plugin skill must be UTF-8 text: {name}"
        ) from exc
    if "\x00" in text:
        raise ClaudePluginCapabilityError(f"Claude plugin skill must be text: {name}")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        frontmatter = text[3:end] if end >= 0 else text[3:]
        if _FORBIDDEN_SKILL_METADATA.search(frontmatter):
            raise ClaudePluginCapabilityError(
                f"Claude plugin skill may not grant tools or hooks through frontmatter: {name}"
            )


def _validate_vendor_receipt(path: Path, root: Path, files: set[Path]) -> None:
    if not path.is_file() or path.stat().st_size > _MAX_SKILL_BYTES:
        raise ClaudePluginCapabilityError("Claude plugin vendor receipt must be a bounded file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, RecursionError, json.JSONDecodeError) as exc:
        raise ClaudePluginCapabilityError("Claude plugin vendor receipt must be valid JSON") from exc
    required = {"format_version", "capability_id", "kind", "source_tree_sha256", "files"}
    rows = [
        {
            "path": item.relative_to(root).as_posix(),
            "bytes": item.stat().st_size,
            "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
        }
        for item in sorted(files, key=lambda candidate: candidate.relative_to(root).as_posix())
    ]
    tree = hashlib.sha256()
    for row in rows:
        tree.update(str(row["path"]).encode("utf-8"))
        tree.update(b"\0")
        tree.update(str(row["sha256"]).encode("ascii"))
        tree.update(b"\n")
    if (
        not isinstance(payload, dict)
        or not required.issubset(payload)
        or payload.get("format_version") != 1
        or payload.get("kind") != "claude-plugin"
        or payload.get("files") != rows
        or payload.get("source_tree_sha256") != tree.hexdigest()
    ):
        raise ClaudePluginCapabilityError("Claude plugin vendor receipt has an invalid schema")


def _manifest(path: Path) -> dict[str, object]:
    if not path.is_file() or path.stat().st_size > _MAX_SKILL_BYTES:
        raise ClaudePluginCapabilityError("Claude plugin manifest must be a bounded file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, RecursionError, json.JSONDecodeError) as exc:
        raise ClaudePluginCapabilityError(f"cannot load Claude plugin manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise ClaudePluginCapabilityError("Claude plugin manifest root must be an object")
    unsupported = sorted(set(payload) - _SAFE_MANIFEST_FIELDS)
    if unsupported:
        raise ClaudePluginCapabilityError(
            "Claude plugin manifest contains unsupported runtime or configuration fields: "
            + ", ".join(unsupported)
        )
    if "name" not in payload:
        raise ClaudePluginCapabilityError("Claude plugin manifest name is required when plugin.json exists")
    return payload


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except ValueError:
        return False
