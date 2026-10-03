from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .attribution import GitAttribution
from .git import GitError, GitWorkspace
from .persistence import Store


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
    if target == root or root in target.parents or target in root.parents:
        raise GitError(f"refusing to run task {task_id} outside an isolated worktree: {target} overlaps {root}")
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


NO_IMPLEMENTATION_CHANGE = "no_implementation_change"


def record_task_baseline(store: Store, task_id: str, run_path: Path) -> str:
    """Capture the task's starting SHA before the first provider run; it never changes afterwards."""
    existing = store.task_baseline(task_id)
    if existing is not None:
        return existing
    return store.set_task_baseline(task_id, GitWorkspace(run_path).head())


def commit_implementation_candidate(
    store: Store,
    task_id: str,
    run_path: Path,
    baseline_sha: str,
    message: str,
    attribution: GitAttribution | None = None,
) -> str | None:
    """Commit the worktree and return the candidate SHA, or None when it is not a real new change."""
    workspace = GitWorkspace(run_path)
    sha = workspace.commit_all(message, attribution=attribution)
    if sha.startswith("synthetic-") or sha == baseline_sha:
        return None
    if workspace.run("diff", "--quiet", baseline_sha, sha, check=False).returncode == 0:
        return None
    known = {row["sha"] for row in store.conn.execute("SELECT sha FROM candidates WHERE task_id=?", (task_id,))}
    return None if sha in known else sha
