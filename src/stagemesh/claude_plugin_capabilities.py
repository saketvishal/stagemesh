"""Safe import of skills-only Claude Code plugins.

StageMesh deliberately does not activate Claude's plugin runtime.  It validates
one project-local plugin directory and exposes only its ``skills/*/SKILL.md``
files through the existing read-only skill adapter.  Runtime components remain
unsupported because they can execute with the worker account's authority.
"""

from __future__ import annotations

import hashlib
import json
import re
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


class ClaudePluginCapabilityError(RuntimeError):
    """A Claude plugin cannot be imported through the read-only adapter."""


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
            "activation": "skills-only-import",
            "skills": [skill.private_payload() for skill in self.skills],
        }


def load_claude_plugin(path: Path, project: Path) -> ClaudePluginCapability:
    """Validate a project-local skills-only plugin without executing its runtime."""
    root = Path(path).resolve()
    project_root = Path(project).resolve()
    if not root.is_dir() or not _inside(root, project_root):
        raise ClaudePluginCapabilityError("Claude plugin source must be a directory inside the project")

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
        skills.append(ClaudePluginSkill(name, skill_path, hashlib.sha256(data).hexdigest()))
    if not skills:
        raise ClaudePluginCapabilityError("skills-only Claude plugin must contain at least one SKILL.md")
    if len(skills) > _MAX_SKILLS:
        raise ClaudePluginCapabilityError(f"Claude plugin may expose at most {_MAX_SKILLS} skills")

    aggregate = hashlib.sha256()
    for skill in skills:
        aggregate.update(skill.name.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(skill.content_sha256.encode("ascii"))
        aggregate.update(b"\n")
    return ClaudePluginCapability(plugin_name, root, tuple(skills), aggregate.hexdigest())


def _manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
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
