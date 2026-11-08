"""Independent, exact-SHA candidate diff inspection and quarantine evidence.

The implementation provider's narrative is never an input.  This module reads
Git state directly and emits a bounded, provider-neutral report used before
review or promotion.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from pathlib import Path
from .agent_policy import agent_policy_findings
from .audit import record_audit
from .contracts import ChangeContract, scope_classification, scope_policy_hash
from .git import GitWorkspace
from .persistence import Store

_GATE_PATTERNS = (
    ".github/**",
    ".stagemesh/**",
    "stagemesh.contract.json",
    "pyproject.toml",
    "package.json",
    "scripts/**",
)
_MAX_PATHS = 200
_RAW_MODE = re.compile(r"^:(?P<old>[0-7]{6}) (?P<new>[0-7]{6}) ")


def inspect_candidate_diff(
    project: Path,
    baseline_sha: str,
    candidate_sha: str,
    contract: ChangeContract,
    *,
    workspace: Path | None = None,
) -> dict[str, object]:
    git = GitWorkspace(project)
    baseline = git.run("rev-parse", f"{baseline_sha}^{{commit}}").stdout.strip()
    candidate = git.run("rev-parse", f"{candidate_sha}^{{commit}}").stdout.strip()
    entries = _name_status(git.run("diff", "--name-status", "-z", "-M", baseline, candidate, encoding="utf-8").stdout)
    paths = sorted({path for entry in entries for path in _entry_paths(entry)})
    scope = scope_classification(contract, paths)
    additions, deletions, binary = _numstat(git.run("diff", "--numstat", baseline, candidate, encoding="utf-8").stdout)
    raw = git.run("diff", "--raw", "-M", baseline, candidate, encoding="utf-8").stdout
    special_modes = _special_modes(raw)
    workspace_state = inspect_workspace_state(workspace, candidate) if workspace is not None else {"status": "not_supplied"}
    agent_policy, policy_findings = agent_policy_findings(
        project, baseline, candidate, contract, paths, entries
    )
    blocking = sorted(
        {
            *scope["outside_allowed"],
            *scope["excluded"],
            *scope["forbidden"],
            *scope["protected"],
        }
    )
    if workspace_state.get("status") == "dirty_or_mismatched":
        blocking.append("<task-worktree-state>")
    if any(item["severity"] == "error" for item in policy_findings):
        blocking.append("<agent-coding-policy>")
    risk_flags: list[str] = []
    if any(_matches(path, _GATE_PATTERNS) for path in paths):
        risk_flags.append("gate_configuration_changed")
    if binary:
        risk_flags.append("binary_change")
    if special_modes:
        risk_flags.append("symlink_or_submodule_change")
    if contract.max_changed_files is not None and len(paths) > contract.max_changed_files:
        risk_flags.append("changed_file_expectation_exceeded")
    total_lines = additions + deletions
    if contract.max_diff_lines is not None and total_lines > contract.max_diff_lines:
        risk_flags.append("diff_line_expectation_exceeded")
    if any(item["severity"] == "review" for item in policy_findings):
        risk_flags.append("agent_policy_review_required")
    disposition = "QUARANTINED" if blocking else ("REVIEW_REQUIRED" if risk_flags else "ACCEPTABLE")
    report: dict[str, object] = {
        "schema": "stagemesh.candidate-diff-inspection/1",
        "baseline_sha": baseline,
        "candidate_sha": candidate,
        "contract_scope_hash": scope_policy_hash(contract) if contract.scope_mode else None,
        "agent_policy": {**agent_policy.payload(), "policy_hash": agent_policy.digest},
        "agent_policy_findings": policy_findings,
        "entries": entries[:_MAX_PATHS],
        "changed_paths": paths[:_MAX_PATHS],
        "changed_path_count": len(paths),
        "additions": additions,
        "deletions": deletions,
        "binary_paths": binary[:_MAX_PATHS],
        "special_modes": special_modes[:_MAX_PATHS],
        "scope": scope,
        "gate_configuration_paths": sorted(path for path in paths if _matches(path, _GATE_PATTERNS))[:_MAX_PATHS],
        "workspace_state": workspace_state,
        "risk_flags": risk_flags,
        "blocking_paths": blocking[:_MAX_PATHS],
        "disposition": disposition,
    }
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
    report["inspection_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return report


def inspect_workspace_state(workspace: Path, candidate_sha: str) -> dict[str, object]:
    path = Path(workspace)
    if not path.is_dir() or not (path / ".git").exists():
        return {"status": "unavailable", "path": str(path)}
    git = GitWorkspace(path)
    head = git.run("rev-parse", "HEAD", check=False).stdout.strip()
    staged = _zpaths(git.run("diff", "--cached", "--name-only", "-z", encoding="utf-8", check=False).stdout)
    unstaged = _zpaths(git.run("diff", "--name-only", "-z", encoding="utf-8", check=False).stdout)
    untracked = _zpaths(git.run("ls-files", "--others", "--exclude-standard", "-z", encoding="utf-8", check=False).stdout)
    ignored = _zpaths(
        git.run("ls-files", "--others", "--ignored", "--exclude-standard", "-z", encoding="utf-8", check=False).stdout
    )
    dirty = bool(staged or unstaged or untracked or head != candidate_sha)
    return {
        "status": "dirty_or_mismatched" if dirty else "clean",
        "path": str(path.resolve()),
        "head": head,
        "expected_head": candidate_sha,
        "staged": staged[:_MAX_PATHS],
        "unstaged": unstaged[:_MAX_PATHS],
        "untracked": untracked[:_MAX_PATHS],
        "ignored_not_in_candidate": ignored[:_MAX_PATHS],
        "ignored_count": len(ignored),
    }


def quarantine_candidate(store: Store, project: Path, task_id: str, report: dict[str, object]) -> str:
    candidate = str(report["candidate_sha"])
    task_key = hashlib.sha1(task_id.encode("utf-8")).hexdigest()[:12]
    ref = f"refs/stagemesh/quarantine/{task_key}/{candidate[:12]}"
    GitWorkspace(project).run("update-ref", ref, candidate)
    record_audit(
        store,
        "candidate.diff_quarantined",
        {"task_id": task_id, "quarantine_ref": ref, "inspection": report},
    )
    return ref


def record_candidate_inspection(store: Store, task_id: str, report: dict[str, object]) -> None:
    record_audit(store, "candidate.diff_inspected", {"task_id": task_id, "inspection": report})


def _name_status(output: str) -> list[dict[str, object]]:
    tokens = [token for token in output.split("\0") if token]
    entries: list[dict[str, object]] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        width = 2 if status.startswith(("R", "C")) else 1
        paths = tokens[index + 1:index + 1 + width]
        if len(paths) != width:
            break
        entries.append({"status": status, "paths": [path.replace("\\", "/") for path in paths]})
        index += 1 + width
    return entries


def _entry_paths(entry: dict[str, object]) -> list[str]:
    paths = entry.get("paths")
    if not isinstance(paths, list):
        return []
    return [str(path) for path in paths]


def _numstat(output: str) -> tuple[int, int, list[str]]:
    additions = deletions = 0
    binary: list[str] = []
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if added == "-" or deleted == "-":
            binary.append(path.replace("\\", "/"))
            continue
        if added.isdigit() and deleted.isdigit():
            additions += int(added)
            deletions += int(deleted)
    return additions, deletions, sorted(binary)


def _special_modes(output: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        match = _RAW_MODE.match(line)
        if match is None or not ({match["old"], match["new"]} & {"120000", "160000"}):
            continue
        path = line.rsplit("\t", 1)[-1].replace("\\", "/")
        rows.append({"path": path, "old_mode": match["old"], "new_mode": match["new"]})
    return rows


def _zpaths(output: str) -> list[str]:
    return sorted(token.replace("\\", "/") for token in output.split("\0") if token)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    return any(
        fnmatch.fnmatchcase(normalized, pattern) or fnmatch.fnmatchcase(normalized.casefold(), pattern.casefold())
        for pattern in patterns
    )
