"""Narrow operator-config worktree validation.

Task-provided text must never choose the executor cwd. Only trusted
WorkerConfig paths are validated here.
"""

from __future__ import annotations

from pathlib import Path


class WorktreeValidationError(ValueError):
    """Raised when a configured worktree is not safe to use as cwd."""


def validate_worktree_path(
    path: str | None,
    *,
    allowed_roots: tuple[str, ...] = (),
    require_git: bool = True,
) -> Path | None:
    if path is None or path == "":
        return None
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise WorktreeValidationError(f"worktree path does not exist: {resolved}")
    if not resolved.is_dir():
        raise WorktreeValidationError(f"worktree path is not a directory: {resolved}")
    if require_git and not is_git_worktree(resolved):
        raise WorktreeValidationError(
            f"worktree path is not a Git repository or worktree: {resolved}"
        )
    if allowed_roots:
        if not any(_is_under(resolved, root) for root in allowed_roots):
            raise WorktreeValidationError(
                f"worktree path is outside allowed workspace roots: {resolved}"
            )
    return resolved


def is_git_worktree(path: Path) -> bool:
    git_entry = path / ".git"
    if git_entry.is_dir():
        return True
    if git_entry.is_file():
        try:
            contents = git_entry.read_text(encoding="utf-8", errors="replace")[:240]
        except OSError:
            return False
        return contents.lower().startswith("gitdir:")
    return False


def _is_under(path: Path, root: str) -> bool:
    try:
        path.relative_to(Path(root).expanduser().resolve())
        return True
    except ValueError:
        return False
