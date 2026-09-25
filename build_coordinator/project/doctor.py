"""`stagemesh doctor`: explain what works, what does not, and what to do next.

Every check returns a status (OK / WARN / FAIL / INFO), what was found, and, when
it is not OK, the concrete next step. A runtime is only reported READY if a live
headless probe succeeded during `stagemesh agent setup`."""

from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from build_coordinator import __version__
from build_coordinator.agents import machine
from build_coordinator.agents.profiles import PROFILES, READY
from build_coordinator.project.backlog import BacklogError, load_backlog
from build_coordinator.project.definition import (
    ProjectDefinition,
    ProjectError,
    registered_roots,
    registry_path,
    load_project,
)
from build_coordinator.project.state_migration import plan_migration

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"


@dataclass
class Check:
    area: str
    name: str
    status: str
    detail: str
    hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"area": self.area, "check": self.name, "status": self.status, "detail": self.detail, "hint": self.hint}


def _git(cwd: str | Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def check_installation() -> list[Check]:
    checks = [
        Check("install", "version", INFO, f"stagemesh {__version__} (python {platform.python_version()}, {platform.system()})"),
    ]
    on_path = shutil.which("stagemesh")
    checks.append(
        Check("install", "command on PATH", OK, on_path)
        if on_path
        else Check("install", "command on PATH", WARN, "`stagemesh` is not on PATH", "pip install stagemesh (or add the Python Scripts directory to PATH)")
    )
    code, out = _git(".", "--version")
    checks.append(
        Check("install", "git", OK, out)
        if code == 0
        else Check("install", "git", FAIL, "git is not available", "install git and make sure it is on PATH")
    )
    return checks


def check_runtimes() -> list[Check]:
    rows = {row["runtime_id"]: row for row in machine.known_statuses()}
    checks: list[Check] = []
    if not rows:
        return [
            Check(
                "runtimes",
                "agent setup",
                WARN,
                "no agent runtimes have been verified on this machine",
                "run `stagemesh agent setup` to detect and verify installed coding agents",
            )
        ]
    for runtime_id in PROFILES:
        row = rows.get(runtime_id)
        if row is None:
            checks.append(Check("runtimes", runtime_id, INFO, "not checked yet", "run `stagemesh agent setup`"))
            continue
        age = f"verified {row['age_seconds'] // 3600}h ago" if row["age_seconds"] >= 3600 else "verified recently"
        if row["state"] == READY:
            checks.append(Check("runtimes", runtime_id, OK, f"READY: {row.get('version') or ''} ({age}); {row.get('detail', '')}"))
        else:
            hints = {
                "NOT_INSTALLED": f"install {PROFILES[runtime_id].display} if you want to use it",
                "NOT_AUTHENTICATED": f"log in with the {runtime_id} CLI, then run `stagemesh agent setup`",
                "NOT_HEADLESS": "this tool has no non-interactive mode, so StageMesh will not use it",
                "HEADLESS_FAILED": f"run the {runtime_id} CLI once by hand, then `stagemesh agent setup`",
                "DISABLED": f"re-enable with `stagemesh agent enable {runtime_id}`",
            }
            checks.append(Check("runtimes", runtime_id, WARN, f"{row['state']}: {row.get('detail', '')}", hints.get(row["state"], "")))
    if not machine.ready_runtime_ids():
        checks.append(Check("runtimes", "usable agent", FAIL, "no runtime is READY", "log in to at least one agent CLI and run `stagemesh agent setup`"))
    return checks


def check_registry() -> list[Check]:
    path = registry_path()
    roots = registered_roots()
    if not roots:
        return [Check("registry", "projects", WARN, f"no projects registered ({path})", "run `stagemesh project add <path>` (or `stagemesh init` inside a repo)")]
    return [Check("registry", "projects", OK, f"{len(roots)} registered in {path}")]


def check_project(project: ProjectDefinition) -> list[Check]:
    area = f"project:{project.project_id}"
    checks = [Check(area, "discovery", OK, f"{project.root} (.stagemesh/project.yaml valid)")]
    checks.append(
        Check(area, "concurrency", INFO, f"{project.concurrency} builder(s), {project.reviewers} reviewer(s), default review {project.default_review_policy}")
    )
    try:
        definitions = load_backlog(project)
        checks.append(Check(area, "backlog", OK if definitions else WARN, f"{len(definitions)} task definition(s)", "" if definitions else "add tasks under .stagemesh/tasks/"))
        if any(d.review_policy == "TWO_REVIEWERS" for d in definitions) and project.reviewers < 2:
            checks.append(Check(area, "review policy", FAIL, "TWO_REVIEWERS tasks need execution.reviewers >= 2", "set execution.reviewers: 2 in project.yaml"))
    except BacklogError as exc:
        checks.append(Check(area, "backlog", FAIL, "; ".join(exc.problems)[:400], "fix the task definitions listed above, then re-run doctor"))
    except ProjectError as exc:
        checks.append(Check(area, "backlog", FAIL, str(exc)[:300]))

    code, out = _git(project.root, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        checks.append(Check(area, "git", FAIL, f"{project.root} is not a git repository", "run `git init` and commit the project"))
        return checks
    code, branch = _git(project.root, "rev-parse", "--verify", "--quiet", f"refs/heads/{project.main_ref}")
    checks.append(
        Check(area, "main branch", OK, f"{project.main_ref} exists")
        if code == 0
        else Check(area, "main branch", FAIL, f"branch {project.main_ref!r} does not exist", f"create it or set repository.main_ref in project.yaml")
    )
    if project.upstream_remote:
        code, _ = _git(project.root, "remote", "get-url", project.upstream_remote)
        checks.append(
            Check(area, "upstream", OK, f"remote {project.upstream_remote}; push={'on' if project.push_upstream else 'off'}")
            if code == 0
            else Check(area, "upstream", FAIL, f"git remote {project.upstream_remote!r} is not configured", f"git remote add {project.upstream_remote} <url>, or remove `upstream` from project.yaml")
        )
    else:
        checks.append(Check(area, "upstream", INFO, "local only: integrated work stays on the local main branch, nothing is pushed"))

    ignored, _ = _git(project.root, "check-ignore", "-q", str(project.state_dir))
    checks.append(
        Check(area, "runtime state ignored by git", OK, str(project.state_dir))
        if ignored == 0 or not project.state_dir.is_relative_to(project.root)
        else Check(area, "runtime state ignored by git", WARN, f"{project.state_dir} is not git-ignored", "add it to .gitignore so runtime state is never committed")
    )

    db = project.state_dir / "coordinator.sqlite3"
    if not db.exists():
        checks.append(Check(area, "durable state", INFO, "not created yet (created by the first `stagemesh continue`)"))
    else:
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
                if "build_coordinator_schema_version" not in tables:
                    checks.append(Check(area, "durable state", FAIL, "database predates schema versioning", "run `stagemesh project migrate-state --all --apply` (backs up first)"))
                else:
                    counts = dict(conn.execute("select state, count(*) from build_tasks group by state").fetchall())
                    version = conn.execute("select version from build_coordinator_schema_version").fetchone()[0]
                    migration = plan_migration(db)
                    if migration.needed:
                        changes = ", ".join(f"{item['action']} {item['table']}" for item in migration.tables[:5])
                        checks.append(Check(area, "durable state", FAIL, f"schema v{version} requires migration/repair: {changes or 'version stamp'}", "run `stagemesh project migrate-state --all --apply` (backs up first)"))
                    else:
                        checks.append(Check(area, "durable state", OK, f"schema v{version}; tasks {counts or 'none'}"))
                    blocked = conn.execute(
                        "select task_id, event_data from build_task_events e where to_state='BLOCKED' and rowid = "
                        "(select max(rowid) from build_task_events where task_id=e.task_id) and task_id in "
                        "(select task_id from build_tasks where state='BLOCKED')"
                    ).fetchall()
                    if blocked:
                        checks.append(Check(area, "human action", WARN, f"{len(blocked)} task(s) blocked: " + ", ".join(t for t, _ in blocked[:6]), f"see `stagemesh project status {project.project_id}`"))
        except sqlite3.Error as exc:
            checks.append(Check(area, "durable state", FAIL, f"cannot read {db}: {exc}"))

    wt = project.worktrees_dir
    if wt.exists():
        code, listing = _git(project.root, "worktree", "list", "--porcelain")
        managed = [ln for ln in listing.splitlines() if ln.startswith("worktree ") and str(wt).replace("\\", "/").lower() in ln.replace("\\", "/").lower()]
        checks.append(Check(area, "worktrees", OK, f"{len(managed)} managed workspace(s) under {wt}"))
    return checks


def run_doctor(project_names: list[ProjectDefinition] | None = None) -> dict[str, Any]:
    checks = check_installation() + check_registry() + check_runtimes()
    projects = project_names
    if projects is None:
        projects = []
        for root in registered_roots():
            try:
                projects.append(load_project(root))
            except ProjectError as exc:
                checks.append(Check("registry", str(root), FAIL, str(exc)[:300], "fix or remove it: `stagemesh project remove <path>`"))
    for project in projects:
        checks.extend(check_project(project))
    failing = [c for c in checks if c.status == FAIL]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "healthy": not failing,
        "counts": {s: sum(1 for c in checks if c.status == s) for s in (OK, INFO, WARN, FAIL)},
        "checks": [c.as_dict() for c in checks],
    }


def format_report(report: dict[str, Any]) -> str:
    marks = {OK: "[ ok ]", INFO: "[info]", WARN: "[warn]", FAIL: "[FAIL]"}
    lines = []
    area = None
    for check in report["checks"]:
        if check["area"] != area:
            area = check["area"]
            lines.append(f"\n{area}")
        lines.append(f"  {marks[check['status']]} {check['check']}: {check['detail']}")
        if check["hint"]:
            lines.append(f"         -> {check['hint']}")
    lines.append(f"\n{'healthy' if report['healthy'] else 'NOT healthy'}: {report['counts']}")
    return "\n".join(lines)
