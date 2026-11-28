"""Read a reviewed file-operation list from Git for an operator-authorized scope rebind."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeGuard

from .contracts import ChangeContract, ContractError, _matches
from .git import GitError, GitWorkspace


def manifest_contract(
    project: Path,
    baseline: str,
    reference: str,
    contract: ChangeContract,
    *,
    scope_owner: str,
    allow_workflow_modification: bool,
) -> ChangeContract:
    entries = _read_scope(project, baseline, reference)
    _, _, path = reference.partition(":")
    paths = tuple(target for target, _ in entries)
    if path.casefold() in {target.casefold() for target in paths} or any(_matches(target, contract.exclusions) for target in paths):
        raise ContractError("manifest requests an excluded file or the governing manifest itself")
    if contract.scope_mode and any(target not in contract.allowed_files for target in paths):
        raise ContractError("the manifest would expand an existing atomic scope; use the owner-reviewed scope revision process")
    if contract.allowed_generated_files or contract.approved_scope_expansions:
        raise ContractError("the contract has separate write authority that must be reconciled before importing a manifest")
    forbidden = contract.forbidden_files
    workflows = [(target, operation) for target, operation in entries if _matches(target, (".github/**",))]
    if workflows:
        if not allow_workflow_modification or any(
            not re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", target) or operation != "modify" for target, operation in workflows
        ):
            raise ContractError("manifest scope requires explicit permission for modifications to existing GitHub workflows only")
        forbidden = tuple(rule for rule in forbidden if rule != ".github/**")
    revised = replace(
        contract,
        allowed_files=paths,
        forbidden_files=forbidden,
        max_changed_files=min(contract.max_changed_files or len(paths), len(paths)),
        scope_mode="STRICT",
        scope_owner=contract.scope_owner or scope_owner,
        scope_revision=contract.scope_revision or 1,
        scope_file_operations=entries,
        scope_manifest_ref=reference,
    )
    if contract.scope_revision and revised != contract:
        revised = replace(revised, scope_revision=contract.scope_revision + 1)
    return revised


def validate_manifest_binding(project: Path, baseline: str, contract: ChangeContract) -> None:
    if contract.scope_manifest_ref and _read_scope(project, baseline, contract.scope_manifest_ref) != contract.scope_file_operations:
        raise ContractError("frozen file operations disagree with the approved manifest; rebind the task")


def _read_scope(project: Path, baseline: str, reference: str) -> tuple[tuple[str, str], ...]:
    revision, _, path = reference.partition(":")
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", revision) or not _literal_path(path):
        raise ContractError("manifest reference must be FULL_COMMIT:path/to/manifest.json")
    git = GitWorkspace(project)
    try:
        return _scope_at_baseline(git, revision, baseline, path)
    except (GitError, OSError, UnicodeDecodeError) as exc:
        raise ContractError("Git could not read the approved scope manifest and frozen baseline") from exc


def _scope_at_baseline(git: GitWorkspace, revision: str, baseline: str, path: str) -> tuple[tuple[str, str], ...]:
    if git.run("merge-base", "--is-ancestor", revision, baseline, check=False).returncode:
        raise ContractError("the approved manifest commit must be an ancestor of the frozen task baseline")
    approved = _tree_entry(git, revision, path)
    if approved is None or approved[0] not in ("100644", "100755") or _tree_entry(git, baseline, path) != approved:
        raise ContractError("the approved scope manifest is missing or has changed at the task baseline")
    size = int(git.run("cat-file", "-s", approved[1]).stdout.strip())
    if size > 64_000:
        raise ContractError("scope manifest exceeds the scope input limit")
    entries = _operations(git.run("cat-file", "blob", approved[1], encoding="utf-8").stdout)
    for target, operation in entries:
        existing = _tree_entry(git, baseline, target)
        if (operation == "create" and existing is not None) or (
            operation != "create" and (existing is None or existing[0] not in ("100644", "100755"))
        ):
            raise ContractError(f"manifest operation {operation} conflicts with the task baseline for {target}")
    return entries


def _tree_entry(git: GitWorkspace, revision: str, path: str) -> tuple[str, str] | None:
    line = git.run("--literal-pathspecs", "ls-tree", "-z", revision, "--", path, encoding="utf-8").stdout.rstrip("\0")
    if not line:
        return None
    metadata, _, name = line.partition("\t")
    mode, _, oid = metadata.split()
    return (mode, oid) if name == path else None


def _operations(source: str) -> tuple[tuple[str, str], ...]:
    try:
        content = json.loads(source, object_pairs_hook=_distinct_fields)
    except ValueError as exc:
        raise ContractError("implementation manifest must contain valid JSON") from exc
    files = content.get("files") if isinstance(content, dict) and content.get("scope", "direct") == "direct" else None
    if not isinstance(files, list) or not files:
        raise ContractError("implementation manifest must declare a nonempty direct file scope")
    paths: dict[str, str] = {}
    seen: set[str] = set()
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else None
        operation = entry.get("operation") if isinstance(entry, dict) else None
        if not _literal_path(path) or path.casefold() in seen or operation not in ("create", "modify", "delete"):
            raise ContractError("implementation manifest requires distinct literal paths with create, modify or delete operations")
        seen.add(path.casefold())
        paths[path] = operation
    return tuple(sorted(paths.items()))


def _distinct_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate manifest keys")
    return fields


def _literal_path(path: object) -> TypeGuard[str]:
    return bool(
        isinstance(path, str) and path.strip() == path and len(path) <= 1024
        and not any(c in path for c in '\\:*?[]<>|"\r\n\t\0')
        and all(part not in ("", ".", "..") and not part.endswith((" ", ".")) for part in path.split("/"))
    )
