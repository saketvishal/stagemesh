from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
from pathlib import Path

from .attribution import GitAttribution
from .config import load_config
from .git import GitError, GitWorkspace
from .persistence import Store


_GENERATION_DIR = re.compile(r"^[0-9a-f]{12}-g\d+$")

# `git worktree add` and the config writes that follow it touch shared repository files; parallel tasks take turns.
_WORKTREE_CREATION = threading.Lock()


def _task_key(task_id: str) -> str:
    return hashlib.sha1(task_id.encode("utf-8")).hexdigest()[:12]


def worktree_root(project: Path) -> Path:
    config = load_config(Path(project).resolve())
    assert config.runtime is not None
    return config.runtime.worktree_root


def task_workspace(project: Path, task_id: str, root: Path | None = None) -> Path:
    base = Path(root).resolve() if root is not None else worktree_root(project)
    key = _task_key(task_id)
    generation = worktree_generation(base, key)
    return base / (f"{key}-g{generation}" if generation else key)


def worktree_generation(root: Path, key: str) -> int:
    """The active worktree generation of a task: 0 normally, higher after a fence replaced an execution of unknown identity."""
    try:
        return max(0, int((Path(root) / f"{key}.generation").read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def advance_worktree_generation(project: Path, task_id: str) -> tuple[Path, Path]:
    """Point the task at a fresh worktree path; the previous one is left exactly as it is. Returns (old, new) paths."""
    root = worktree_root(project)
    key = _task_key(task_id)
    old = task_workspace(project, task_id, root)
    generation = worktree_generation(root, key) + 1
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{key}.generation").write_text(str(generation), encoding="utf-8")
    return old, root / f"{key}-g{generation}"


def prepare_task_workspace(project: Path, task_id: str) -> Path:
    root = Path(project).resolve()
    workspace = GitWorkspace(root)
    workspace.init_if_needed()
    runtime_root = worktree_root(root)
    _ensure_worktree_excluded(root, runtime_root)
    _ensure_head(workspace, root)
    target = task_workspace(root, task_id, runtime_root)
    overlaps_checkout = target == root or target in root.parents or (
        root in target.parents and not _under_runtime_dir(root, target)
    )
    if overlaps_checkout:
        raise GitError(
            f"refusing to run task {task_id} outside a validated runtime worktree root: "
            f"{target} overlaps {root}"
        )
    with _WORKTREE_CREATION:
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
    with _WORKTREE_CREATION:
        try:
            GitWorkspace(project).run("worktree", "remove", "--force", str(target))
        except GitError:
            shutil.rmtree(target, ignore_errors=True)


def sweep_task_worktrees(project: Path, store: Store) -> list[dict[str, str]]:
    """Remove worktrees nothing can resume: those of finished tasks and directories no known task owns.

    Worktrees of unfinished tasks are deliberately kept - a restarted run reuses them (and their commits) rather than
    starting over. Stale git worktree registrations are pruned either way.
    """
    root = worktree_root(project)
    git = GitWorkspace(project)
    git.run("worktree", "prune", check=False)
    if not root.is_dir():
        return []
    tasks = {_task_key(str(row["id"])): row for row in store.tasks()}
    actions: list[dict[str, str]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        row = tasks.get(entry.name.split("-g", 1)[0] if _GENERATION_DIR.match(entry.name) else entry.name)
        if row is None:
            reason, task_id = "no task owns this worktree", None
        elif row["status"] == "DONE" or row["stage"] == "DONE":
            reason, task_id = "task is done", str(row["id"])
        else:
            continue
        with _WORKTREE_CREATION:
            if git.run("worktree", "remove", "--force", str(entry), check=False).returncode != 0:
                shutil.rmtree(entry, ignore_errors=True)
        actions.append({"task_id": task_id or "", "worktree": str(entry), "action": "REMOVED", "reason": reason})
    git.run("worktree", "prune", check=False)
    return actions


def legacy_worktree_roots(project: Path) -> list[Path]:
    """Known legacy roots operators may inspect manually; StageMesh never removes them automatically."""
    project = Path(project).resolve()
    digest = hashlib.sha1(str(project).encode("utf-8")).hexdigest()[:10]
    candidates = [
        Path(project.anchor) / ".sm-wt" / digest,
        project.parent / ".sm-wt" / digest,
    ]
    seen: set[str] = set()
    roots: list[Path] = []
    for candidate in candidates:
        key = str(candidate.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            roots.append(candidate)
    return roots


def _under_runtime_dir(project: Path, target: Path) -> bool:
    """True when `target` is the project's runtime dir or below it.

    Judged both lexically and after resolving symlinks: on Windows `Path.resolve()` can transiently answer differently while a
    sibling task thread is creating or removing directories under the same root, which used to refuse a perfectly valid task
    ("overlaps the checkout") and fail it with no progress.
    """
    lexical_runtime, lexical_target = Path(os.path.abspath(project / ".stagemesh")), Path(os.path.abspath(target))
    if lexical_target == lexical_runtime or lexical_runtime in lexical_target.parents:
        return True
    runtime = (project / ".stagemesh").resolve()
    target = target.resolve()
    return target == runtime or runtime in target.parents


def _ensure_head(workspace: GitWorkspace, root: Path) -> None:
    if workspace.run("rev-parse", "--verify", "HEAD", check=False).returncode == 0:
        return
    marker = root / ".stagemesh-root"
    marker.write_text("StageMesh workspace root\n", encoding="utf-8")
    workspace.commit_all("Initialize StageMesh workspace")


def _ensure_worktree_excluded(root: Path, runtime_root: Path) -> None:
    runtime_root = Path(runtime_root).resolve()
    if root not in runtime_root.parents:
        return
    pattern = runtime_root.relative_to(root).as_posix().rstrip("/") + "/"
    exclude = root / ".git" / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if pattern not in {item.strip() for item in existing.splitlines()}:
            with exclude.open("a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(pattern + "\n")
    except OSError as exc:
        raise GitError(f"could not exclude StageMesh worktrees from git: {exc}") from exc


NO_IMPLEMENTATION_CHANGE = "no_implementation_change"


def record_task_baseline(store: Store, task_id: str, run_path: Path) -> str:
    """Capture the task's starting SHA before the first provider run; it never changes afterwards."""
    from .autonomy.hooks import workspace_handed_to_execution  # opt-in supervisor; a no-op unless the project enabled it

    workspace_handed_to_execution(store, task_id, run_path)
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
    from .autonomy.hooks import before_candidate_commit  # opt-in supervisor; a no-op unless the project enabled it

    before_candidate_commit(store, task_id, run_path)  # a second writer's commit is never adopted into the candidate
    workspace = GitWorkspace(run_path)
    sha = workspace.commit_all(message, attribution=attribution)
    if sha.startswith("synthetic-") or sha == baseline_sha:
        return None
    if workspace.run("diff", "--quiet", baseline_sha, sha, check=False).returncode == 0:
        return None
    known = {row["sha"] for row in store.conn.execute("SELECT sha FROM candidates WHERE task_id=?", (task_id,))}
    return None if sha in known else sha
