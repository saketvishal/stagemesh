"""When may a gate that already passed be skipped, and which files may drive automatic validation selection?

Reuse is deliberately narrow. A previously PASSED gate result is reusable only when every input that could change its outcome is
identical, and each of them is part of one fingerprint:

* the candidate's Git *tree* (all content and dependency manifests; a new commit with the same tree is the same content),
* the bound contract digest (a changed contract is a different obligation),
* the declared command, working directory, extra environment and time limit,
* the resolved executable (path and, for a file, size and modification time) and the platform and interpreter,
* a policy version, so a change to these rules invalidates everything recorded under the old ones.

Failed results are never reused. Hosted checks, independent review and the integration-time checks are separate stages that do not
call this module: a reused local result can never stand in for them. Within one attempt, two gates with the same command are run
once and the result is shared (reported as such).

Automatic selection (browser end-to-end, nearby tests, risk classification) is driven by the candidate's *own* change. When the bound
baseline is demonstrably behind the integration ref, files that only appear in the diff because other work landed after that
baseline are reported and kept out of the selection. The bound baseline itself is never replaced, and the contract's scope check
still sees the full diff.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .git import GitError, GitWorkspace

GATE_REUSE_POLICY_VERSION = 1

_BASE_FLAGS = frozenset({"--base", "--base-ref", "--since", "--changed-since", "--changedsince", "--from", "--compare-branch", "--diff-base"})
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REF_LIKE = re.compile(r"^(?:origin/|refs/|upstream/)[\w./\-]+$|^HEAD~\d+$|^[\w./\-]+\.{2,3}[\w./\-]+$")


def candidate_tree(project: Path, candidate_sha: str) -> str | None:
    try:
        return GitWorkspace(project).run("rev-parse", f"{candidate_sha}^{{tree}}").stdout.strip() or None
    except (GitError, OSError):
        return None


def _executable_stamp(gate: Any, search_path: str | None) -> str:
    from .gate_execution import resolve_gate_executable

    resolution = resolve_gate_executable(list(gate.command), Path(os.getcwd()), search_path=search_path)
    if resolution is None or not resolution.executed:
        return "unresolved"
    if resolution.method == "explicit-relative":
        return f"in-tree:{resolution.declared}"  # its bytes are part of the candidate tree
    try:
        info = Path(resolution.executed).stat()
        return f"{resolution.executed}:{info.st_size}:{info.st_mtime_ns}"
    except OSError:
        return f"{resolution.executed}:unstatable"


def gate_input_fingerprint(tree: str, contract_digest: str, gate: Any) -> str:
    environment = dict(gate.env)
    material = {
        "policy": GATE_REUSE_POLICY_VERSION,
        "tree": tree,
        "contract": contract_digest,
        "command": list(gate.command),
        "cwd": gate.cwd or ".",
        "env": sorted(environment.items()),
        "timeout": gate.timeout_seconds,
        "executable": _executable_stamp(gate, environment.get("PATH")),
        "platform": platform.platform(),
        "python": sys.version,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def gate_identity(gate: Any) -> tuple[Any, ...]:
    """Two gates with the same identity do the same work; within one attempt only the first runs."""
    return (tuple(gate.command), gate.cwd or ".", tuple(sorted(dict(gate.env).items())), gate.timeout_seconds)


@dataclass
class GateReuse:
    """Reusable PASSED gate results for one candidate and contract, plus what was actually reused (for the evidence record)."""

    tree: str | None
    contract_digest: str
    prior: Mapping[str, dict[str, Any]] = field(default_factory=dict)  # fingerprint -> {"evidence_id", "gate", "candidate_sha"}
    reused: list[dict[str, Any]] = field(default_factory=list)

    def fingerprint(self, gate: Any) -> str | None:
        return None if self.tree is None else gate_input_fingerprint(self.tree, self.contract_digest, gate)

    def lookup(self, gate: Any) -> tuple[str, dict[str, Any]] | None:
        fingerprint = self.fingerprint(gate)
        if fingerprint is None:
            return None
        hit = self.prior.get(fingerprint)
        return None if hit is None else (fingerprint, hit)


def load_prior_passes(store: Any, task_id: str) -> dict[str, dict[str, Any]]:
    """Fingerprints of gates that PASSED in this task's earlier validation evidence (newest wins)."""
    prior: dict[str, dict[str, Any]] = {}
    rows = store.conn.execute(
        "SELECT id, candidate_sha, payload FROM evidence WHERE task_id=? AND kind='VALIDATION' ORDER BY created_at, rowid", (task_id,)
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        for gate in payload.get("gates", []) if isinstance(payload, dict) else []:
            if isinstance(gate, dict) and gate.get("status") == "PASSED" and isinstance(gate.get("input_fingerprint"), str):
                prior[gate["input_fingerprint"]] = {"evidence_id": str(row["id"]), "gate": str(gate.get("name")), "candidate_sha": str(row["candidate_sha"])}
    return prior


# --- selection ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectionReport:
    """Which baseline measured the change, which files drove automatic selection and which were kept out (with the reason)."""

    baseline_sha: str | None
    baseline_source: str
    inspected_files: tuple[str, ...]
    selection_files: tuple[str, ...]
    excluded_files: tuple[str, ...] = ()
    reason: str = ""
    proposed_baseline_sha: str | None = None
    declared_gate_bases: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_sha": self.baseline_sha,
            "baseline_source": self.baseline_source,
            "inspected_files": list(self.inspected_files),
            "selection_files": list(self.selection_files),
            "excluded_from_selection": list(self.excluded_files),
            "reason": self.reason,
            "proposed_baseline_sha": self.proposed_baseline_sha,
            "declared_gate_bases": [dict(item) for item in self.declared_gate_bases],
        }


def declared_gate_bases(gates: Any, baseline_sha: str | None) -> tuple[dict[str, Any], ...]:
    """Explicit comparison bases frozen inside gate commands. They are reported and preserved, never rewritten."""
    found: list[dict[str, Any]] = []
    for gate in gates:
        words = [str(word) for word in gate.command] + [str(value) for _, value in gate.env]
        for index, word in enumerate(words):
            flag, _, inline = word.partition("=")
            value = inline if inline and flag.lower() in _BASE_FLAGS else (words[index + 1] if word.lower() in _BASE_FLAGS and index + 1 < len(words) else None)
            if value is None and (_SHA.match(word) or _REF_LIKE.match(word)):
                value = word
            if value and value != baseline_sha:
                found.append({"gate": gate.name, "declared_base": value, "bound_baseline": baseline_sha, "preserved": True})
    unique = {tuple(sorted(item.items())): item for item in found}
    return tuple(unique.values())


def _is_ancestor(git: GitWorkspace, older: str, newer: str) -> bool:
    code = git.run("merge-base", "--is-ancestor", older, newer, check=False).returncode
    if code not in (0, 1):  # a missing or corrupt object is "unknown", never "not an ancestor"
        raise GitError(f"cannot establish ancestry of {older} and {newer}")
    return code == 0


def select_files(
    project: Path, candidate_sha: str, baseline_sha: str | None, inspected: tuple[str, ...], integration_ref: str | None, gates: Any = (),
) -> SelectionReport:
    """The files that may drive automatic selection. Without proof that the baseline is stale, that is every inspected file."""
    bases = declared_gate_bases(gates, baseline_sha)
    source = "bound-task-baseline" if baseline_sha else "candidate-parent"
    if not baseline_sha or not integration_ref or not inspected:
        return SelectionReport(baseline_sha, source, inspected, inspected, declared_gate_bases=bases)
    from .contracts import changed_files

    try:
        git = GitWorkspace(project)
        tip = git.run("rev-parse", "--verify", f"{integration_ref}^{{commit}}").stdout.strip()
        if not _is_ancestor(git, baseline_sha, candidate_sha):
            return SelectionReport(baseline_sha, source, inspected, inspected, reason="candidate does not descend from the baseline; no files were excluded", declared_gate_bases=bases)
        if _is_ancestor(git, candidate_sha, tip):
            return SelectionReport(baseline_sha, source, inspected, inspected, reason="candidate is already in the integration ref; no files were excluded", declared_gate_bases=bases)
        base = git.run("merge-base", candidate_sha, tip, check=False)
        merge_base = base.stdout.strip()
        if base.returncode != 0 or not merge_base or merge_base == baseline_sha:
            return SelectionReport(baseline_sha, source, inspected, inspected, declared_gate_bases=bases)
        if not _is_ancestor(git, baseline_sha, merge_base):
            return SelectionReport(baseline_sha, source, inspected, inspected, reason="baseline is not an ancestor of the candidate's merge-base; no files were excluded", declared_gate_bases=bases)
        own = set(changed_files(project, candidate_sha, merge_base))
    except (GitError, OSError):
        return SelectionReport(baseline_sha, source, inspected, inspected, reason="baseline staleness could not be established; no files were excluded", declared_gate_bases=bases)
    excluded = tuple(path for path in inspected if path not in own)
    if not excluded:
        return SelectionReport(baseline_sha, source, inspected, inspected, declared_gate_bases=bases)
    return SelectionReport(
        baseline_sha,
        source,
        inspected,
        tuple(path for path in inspected if path in own),
        excluded,
        f"{len(excluded)} file(s) appear in the diff against the bound baseline only because other work was integrated after it; "
        "they were kept out of automatic validation selection (the bound baseline and the scope check are unchanged)",
        merge_base,
        bases,
    )
