"""Read a reviewed file-operation list from Git for an operator-authorized scope rebind."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import TypeGuard

from .contracts import ChangeContract, ContractError, _matches
from .git import GitWorkspace


def manifest_contract(
    project: Path,
    baseline: str,
    reference: str,
    contract: ChangeContract,
    *,
    scope_owner: str,
    allow_workflow_modification: bool,
) -> ChangeContract:
    revision, _, path = reference.partition(":")
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or not _literal_path(path) or Path(path).name != "implementation-files.json":
        raise ContractError("manifest reference must be FULL_COMMIT:path/to/implementation-files.json")
    git = GitWorkspace(project)
    if git.run("merge-base", "--is-ancestor", revision, baseline, check=False).returncode:
        raise ContractError("the approved manifest commit must be an ancestor of the frozen task baseline")
    blob = git.run("cat-file", "blob", f"{revision}:{path}", encoding="utf-8", check=False)
    current = git.run("cat-file", "blob", f"{baseline}:{path}", encoding="utf-8", check=False)
    if blob.returncode or current.returncode or blob.stdout != current.stdout:
        raise ContractError("the approved implementation manifest is missing or has changed at the task baseline")
    if len(blob.stdout) > 64_000:
        raise ContractError("implementation manifest exceeds the scope input limit")
    entries = _operations(blob.stdout)
    for target, operation in entries:
        existing = git.run("cat-file", "-t", f"{baseline}:{target}", check=False)
        if (operation == "create" and existing.returncode == 0) or (
            operation != "create" and (existing.returncode != 0 or existing.stdout.strip() != "blob")
        ):
            raise ContractError(f"manifest operation {operation} conflicts with the task baseline for {target}")
    paths = tuple(target for target, _ in entries)
    if path.casefold() in {target.casefold() for target in paths} or any(_matches(target, contract.exclusions) for target in paths):
        raise ContractError("manifest requests an excluded file or the governing manifest itself")
    if contract.scope_mode and any(target not in contract.allowed_files for target in paths):
        raise ContractError("the manifest would expand an existing atomic scope; use the owner-reviewed scope revision process")
    if contract.allowed_generated_files or contract.approved_scope_expansions:
        raise ContractError("the contract has separate write authority that must be reconciled before importing a manifest")
    forbidden = contract.forbidden_files
    workflows = [(target, operation) for target, operation in entries if _matches(target, (".github/**",))]
    if allow_workflow_modification and workflows and all(
        re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", target) and operation == "modify" for target, operation in workflows
    ):
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


def _operations(source: str) -> tuple[tuple[str, str], ...]:
    try:
        content = json.loads(source)
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


def _literal_path(path: object) -> TypeGuard[str]:
    return bool(
        isinstance(path, str) and path.strip() == path and len(path) <= 1024
        and not any(c in path for c in '\\:*?[]<>|"\r\n\t\0')
        and all(part not in ("", ".", "..") and not part.endswith((" ", ".")) for part in path.split("/"))
    )
