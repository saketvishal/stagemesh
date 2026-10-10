"""`stagemesh queue-run`: a supervised queue over the parallel runner with strict, contract-driven admission.

On top of ParallelRunner it adds the rules a shared queue needs: a missing contract is auto-planned deterministically (and refused when that cannot be done safely), a task is
refused when the working tree is dirty anywhere it may write, and two tasks never run together when their allowed write scopes
overlap. Selection is conservative (patterns are compared, not files), so it can serialize tasks that would not really collide,
never the reverse.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from .concurrency import ConflictReason, contract_conflict, patterns_overlap
from .config import StageMeshConfig
from .contracts import ChangeContract
from .git import GitError, GitWorkspace
from .parallel import ParallelRunner
from .profile import ProfileError, load_profile
from .profile_smoke import run_smoke
from .run_ready import RunReadyRefusal

RUNTIME_PREFIXES = (".stagemesh/", ".git/")  # StageMesh's own state is not project content


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _covered(pattern: str, forbidden: tuple[str, ...]) -> bool:
    """True when a forbidden pattern swallows the whole allowed pattern (the task cannot write there at all)."""
    p = _norm(pattern)
    return any(fnmatch.fnmatchcase(p, _norm(f)) for f in forbidden)


def write_scope_overlap(first: ChangeContract, second: ChangeContract) -> ConflictReason | None:
    """Allowed write paths two contracts could both modify, after removing what either forbids."""
    for a in first.allowed_files:
        if _covered(a, first.forbidden_files):
            continue
        for b in second.allowed_files:
            if _covered(b, second.forbidden_files):
                continue
            if _covered(a, second.forbidden_files) or _covered(b, first.forbidden_files):
                continue
            if patterns_overlap(a, b):
                return ConflictReason("allowed_paths", a if a == b else f"{a} / {b}")
    return None


def dirty_paths(project: Path) -> list[str]:
    """Modified, staged and untracked paths in the project checkout, excluding StageMesh runtime state."""
    # --no-optional-locks: a status poll must never hold index.lock while another task is integrating
    result = GitWorkspace(project).run("--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", check=False)
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or "git status failed")
    paths = [entry[3:] for entry in result.stdout.split("\0") if len(entry) > 3]
    return sorted(p for p in map(_norm, paths) if not p.startswith(RUNTIME_PREFIXES))


def dirty_in_scope(contract: ChangeContract, dirty: list[str]) -> list[str]:
    allowed = tuple(_norm(p) for p in contract.allowed_files)
    forbidden = tuple(_norm(p) for p in contract.forbidden_files)
    return [
        path
        for path in dirty
        if any(fnmatch.fnmatchcase(path, a) or fnmatch.fnmatchcase(path.casefold(), a.casefold()) for a in allowed)
        and not any(fnmatch.fnmatchcase(path, f) for f in forbidden)
    ]


class QueueRunner(ParallelRunner):
    def __init__(self, *args: Any, **kwargs: Any):
        # Missing contracts are auto-planned by the same deterministic path `continue` uses (unless --no-auto-plan). That path fails
        # closed: with no detectable validation gate, or a contract that does not round-trip, the task is refused, never run unbounded.
        kwargs.setdefault("auto_plan", True)
        kwargs.setdefault("wait_for_providers", True)
        super().__init__(*args, **kwargs)
        self._dirty: list[str] | None = None

    def _refuse_unrunnable(self, summary, skipped, attempted) -> None:  # type: ignore[no-untyped-def]
        for item in skipped:
            if item.get("kind") != "unplannable" or item["task_id"] in attempted:
                continue
            attempted.add(item["task_id"])
            reason = item["reason"]
            if reason.startswith("no contract and auto-planning is disabled"):
                code = "missing_contract"
                message = f"task {item['task_id']} has no change contract; write .stagemesh/contracts/{item['task_id']}.json or rerun without --no-auto-plan"
            elif reason.startswith("no contract and cannot auto-plan"):
                code = "auto_plan_failed"
                message = f"task {item['task_id']}: {reason}; write .stagemesh/contracts/{item['task_id']}.json by hand"
            else:
                code = "invalid_contract"
                message = f"task {item['task_id']}: {reason}"
            self._record_refusal(
                summary, {"task_id": item["task_id"], "contract": item["reason"]}, {"occurred": False, "reused_existing": False, "events": []},
                RunReadyRefusal(code, message, task_id=item["task_id"]),
            )

    def _admit(self, task_id: str, contract: ChangeContract) -> RunReadyRefusal | None:
        try:
            dirty = dirty_paths(self.project)  # fresh each time: another task may just have landed
        except GitError as exc:
            return RunReadyRefusal("git_status_failed", f"cannot inspect the working tree: {exc}", task_id=task_id)
        touched = dirty_in_scope(contract, dirty)
        if touched:
            shown = ", ".join(touched[:10]) + (f" (+{len(touched) - 10} more)" if len(touched) > 10 else "")
            return RunReadyRefusal(
                "dirty_working_tree",
                f"task {task_id} may write paths that have uncommitted changes in the project checkout: {shown}",
                task_id=task_id,
                paths=touched[:50],
            )
        return None

    def _conflict(self, contract: ChangeContract, other: ChangeContract) -> ConflictReason | None:
        return contract_conflict(contract, other) or write_scope_overlap(contract, other)


def preflight(project: Path, config: StageMeshConfig, *, require_ref: bool) -> dict[str, Any]:
    """Project-level checks that must pass before any task is considered. A project with a profile must pass project-smoke."""
    checks: list[dict[str, Any]] = []
    ok = True
    try:
        profile = load_profile(project)
    except ProfileError as exc:
        return {"ok": False, "smoke": {"ran": True, "ok": False, "detail": f"profile unusable: {exc}"}, "checks": checks}
    if profile is None:
        smoke: dict[str, Any] = {"ran": False, "ok": True, "detail": "no project profile; per-task contracts are required instead"}
    else:
        report = run_smoke(project, config)
        smoke = {"ran": True, "ok": report.ok, "profile": report.profile, "checks": [c.to_dict() for c in report.checks if c.status != "skip"]}
        ok = report.ok
    git = GitWorkspace(project)
    inside = git.run("rev-parse", "--git-dir", check=False).returncode == 0
    checks.append({"name": "project is a git repository", "ok": inside})
    ok = ok and inside
    if require_ref:
        ref = config.integration_ref or (git.run("symbolic-ref", "-q", "HEAD", check=False).stdout.strip() if inside else "")
        checks.append({"name": "integration target resolves", "ok": bool(ref), "detail": ref or "no integration_ref configured and no current branch"})
        ok = ok and bool(ref)
    return {"ok": ok, "smoke": smoke, "checks": checks}
