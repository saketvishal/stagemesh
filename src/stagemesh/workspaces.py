from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .git import GitError, GitWorkspace


def task_workspace(project: Path, task_id: str) -> Path:
    root = Path(project).resolve()
    root_key = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:10]
    task_key = hashlib.sha1(task_id.encode("utf-8")).hexdigest()[:12]
    return root.parent / ".sm-wt" / root_key / task_key


def prepare_task_workspace(project: Path, task_id: str) -> Path:
    root = Path(project).resolve()
    workspace = GitWorkspace(root)
    workspace.init_if_needed()
    _ensure_head(workspace, root)
    target = task_workspace(root, task_id)
    if target.exists() and (target / ".git").exists():
        return target
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    workspace.run("worktree", "add", "--detach", str(target), "HEAD")
    if not target.exists() or not (target / ".git").exists():
        raise GitError(f"git worktree was not created at {target}")
    GitWorkspace(target).run("config", "user.email", "stagemesh@example.invalid")
    GitWorkspace(target).run("config", "user.name", "StageMesh")
    return target


def remove_task_workspace(project: Path, task_id: str) -> None:
    target = task_workspace(project, task_id)
    if not target.exists():
        return
    try:
        GitWorkspace(project).run("worktree", "remove", "--force", str(target))
    except GitError:
        shutil.rmtree(target, ignore_errors=True)


def _ensure_head(workspace: GitWorkspace, root: Path) -> None:
    if workspace.run("rev-parse", "--verify", "HEAD", check=False).returncode == 0:
        return
    marker = root / ".stagemesh-root"
    marker.write_text("StageMesh workspace root\n", encoding="utf-8")
    workspace.commit_all("Initialize StageMesh workspace")
