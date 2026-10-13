"""Deterministic, read-only research briefs for implementation prompts."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from .contracts import ChangeContract

MAX_RESEARCH_FILES = 80
MAX_TEST_HINTS = 40
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".stagemesh",
    "__pycache__",
    "dist",
    "node_modules",
}


def build_research_brief(project: Path | None, contract: ChangeContract | None) -> str:
    """Return a scoped, deterministic pre-edit research brief for a provider prompt.

    This is deliberately local and read-only. It gives coding agents a bounded map of
    the files they are allowed to inspect first, without broadening the change scope.
    """
    if project is None or contract is None:
        return ""
    root = project.resolve()
    allowed = _matching_files(root, contract.allowed_files, limit=MAX_RESEARCH_FILES)
    tests = _test_hints(root, allowed, limit=MAX_TEST_HINTS)
    lines = [
        "Contextual research brief (read-only; complete before editing):",
        "- First inspect the existing code paths below and summarize the local pattern in your own scratch notes.",
        "- Use this brief to narrow context. It does not expand the change contract or authorize edits outside allowed files.",
        "- If this brief is missing a path required for the task, stop and request a scope expansion instead of guessing.",
    ]
    if allowed:
        lines.append("- In-scope existing files to inspect first:")
        lines.extend(f"  - {path}" for path in allowed)
    else:
        lines.append("- No existing files matched the allowed scope; inspect the nearest existing parent directories before editing.")
    if tests:
        lines.append("- Nearby tests or fixtures worth checking before coding:")
        lines.extend(f"  - {path}" for path in tests)
    lines.append("")
    return "\n".join(lines) + "\n"


def _matching_files(root: Path, patterns: tuple[str, ...], *, limit: int) -> list[str]:
    files: list[str] = []
    for path in _walk_files(root):
        rel = path.relative_to(root).as_posix()
        if any(_matches(rel, pattern) for pattern in patterns):
            files.append(rel)
            if len(files) >= limit:
                break
    return sorted(files)


def _test_hints(root: Path, allowed: list[str], *, limit: int) -> list[str]:
    if not allowed:
        return []
    stems = {Path(item).stem.lower() for item in allowed if Path(item).stem}
    hints: list[str] = []
    for path in _walk_files(root):
        rel = path.relative_to(root).as_posix()
        parts = set(path.parts)
        if "tests" not in parts and not Path(rel).name.startswith("test_"):
            continue
        name = Path(rel).stem.lower().removeprefix("test_")
        if name in stems or any(stem and stem in name for stem in stems):
            hints.append(rel)
            if len(hints) >= limit:
                break
    return sorted(hints)


def _walk_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        yield path


def _matches(path: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/").strip()
    if not normalized:
        return False
    if normalized == "**":
        return True
    if fnmatch.fnmatch(path, normalized):
        return True
    if normalized.endswith("/**"):
        return path.startswith(normalized[:-3].rstrip("/") + "/")
    return path == normalized
