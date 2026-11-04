"""Read-only structural discovery; no semantic, authorization or review verdicts.

OpenSpec layout-v1 pins this parser's supported grammar, not an upstream package:
openspec/specs/*/spec.md owns active requirements; archived change specs under
openspec/changes/archive/*/specs/*/spec.md are history. Pending deltas are not
canonical owners. Requirements use '### Requirement: Title'; IDs are the path
plus '#requirement-' and a lowercase Markdown-style heading slug. Title/path
renames are remove/add, never an inferred semantic match. Fenced examples are
ignored. Inline Markdown links are explicit references, not inferred dependencies.

CustomSpecAdapter reads an explicitly selected project-local JSON export:
{"schema_version": 1, "owners": [{"id": "policy", "path": "policy.txt",
 "status": "active", "dependencies": [], "requirements": [
 {"id": "POL-1", "text": "Must check", "dependencies": []}]}]}
IDs are globally unique within a snapshot, dependencies name those IDs, and
status is active or history. No plugins, commands, imports or network are run.
The caller supplies snapshots of the chosen base/candidate and their identities;
this module neither selects a candidate nor grants permission to integrate it.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .redaction import SECRET_MARKERS, redact_output
from .spec_refs import (
    Requirement,
    SpecAdapter,
    SpecAdapterError,
    SpecOwner,
    SpecSnapshot,
    local_path,
    nonempty,
    project_file,
)

PINNED_ADAPTERS: dict[str, tuple[str, ...]] = {
    "openspec": ("layout-v1",),
    "custom-json": ("1",),
}

_AUDIT_LIST_LIMIT = 100
_AUDIT_TEXT_LIMIT = 2000
_MANIFEST_RECORD_LIMIT = 1000
_APPROVAL_EVIDENCE_LIMIT = 3200
_VERSION_PATTERN = re.compile(r"(?i)(?:openspec\s+)?v?(\d+\.\d+\.\d+(?:[-+][0-9a-z.-]+)?)")
_EXACT_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_AUDIT_SECRET_ASSIGNMENT = re.compile(
    r'''(?i)(["']?[\w.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|authorization)[\w.-]*["']?)'''
    r'''(\s*[=:]\s*)(?!\*\*\*REDACTED)("[^"]*"|'[^']*'|\S+)'''
)


def resolve_pinned_adapter(adapter_name: str, adapter_version: str | None = None, **kwargs: Any) -> SpecAdapter:
    nonempty(adapter_name, "adapter name")
    allowed_versions = PINNED_ADAPTERS.get(adapter_name)
    if allowed_versions is None:
        raise SpecAdapterError(f"unsupported or unpinned spec adapter: {adapter_name!r}")
    if adapter_version is not None and adapter_version not in allowed_versions:
        raise SpecAdapterError(
            f"unsupported version {adapter_version!r} for pinned adapter {adapter_name!r}; allowed: {allowed_versions}"
        )
    if adapter_name == "openspec":
        return OpenSpecAdapter()
    if adapter_name == "custom-json":
        contract_path = kwargs.get("contract_path")
        if not contract_path:
            raise SpecAdapterError("custom-json adapter requires a contract_path")
        return CustomSpecAdapter(contract_path)
    raise SpecAdapterError(f"unsupported or unpinned spec adapter: {adapter_name!r}")


@dataclass(frozen=True)
class SpecGovernanceVerdict:
    ok: bool
    errors: tuple[str, ...] = ()
    review_findings: tuple[str, ...] = ()
    manifest: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": self.ok,
            "errors": list(self.errors),
            "review_findings": list(self.review_findings),
        }
        if self.manifest is not None:
            result["manifest"] = self.manifest
        return result


@dataclass(frozen=True)
class OpenSpecValidationEvidence:
    ok: bool
    expected_version: str
    observed_version: str | None
    command: tuple[str, ...]
    validation_command: tuple[str, ...] = ()
    exit_code: int | None = None
    sync_status: str = "unknown"
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    errors: tuple[str, ...] = ()
    resolved_executable: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "expected_version": self.expected_version,
            "observed_version": self.observed_version,
            "command": list(self.command),
            "validation_command": list(self.validation_command),
            "exit_code": self.exit_code,
            "sync_status": self.sync_status,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
            "errors": list(self.errors),
            "resolved_executable": self.resolved_executable,
        }


def run_openspec_validation(
    root: Path,
    command: tuple[str, ...] | list[str],
    *,
    expected_version: str,
    timeout_seconds: float = 60.0,
) -> OpenSpecValidationEvidence:
    """Run one explicitly configured OpenSpec binary without a shell or ambient secrets.

    StageMesh owns the fixed read-only arguments. Project manifests cannot add a
    command, and this boundary never installs, updates, syncs or archives specs.
    """
    root = Path(root).resolve()
    prefix = tuple(command)
    if not isinstance(expected_version, str) or _EXACT_VERSION_PATTERN.fullmatch(expected_version) is None:
        return OpenSpecValidationEvidence(
            False, str(expected_version), None, (), errors=("OpenSpec expected_version must be an exact semantic version",)
        )
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 300:
        return OpenSpecValidationEvidence(
            False, expected_version, None, (), errors=("OpenSpec timeout_seconds must be between 0 and 300",)
        )
    if (
        not prefix
        or len(prefix) > 32
        or not all(
            isinstance(item, str)
            and item.strip() == item
            and item
            and len(item) <= 2048
            and not any(char in item for char in ("\x00", "\r", "\n"))
            for item in prefix
        )
    ):
        return OpenSpecValidationEvidence(
            False, expected_version, None, (), errors=("OpenSpec command must be a bounded non-empty argument list",)
        )
    safe_command = _safe_command(prefix)
    resolved_executable = _resolve_spec_executable(root, prefix[0])
    if resolved_executable is None:
        return OpenSpecValidationEvidence(
            False,
            expected_version,
            None,
            safe_command,
            errors=("configured OpenSpec executable could not be resolved",),
        )
    execution_prefix = (resolved_executable, *prefix[1:])
    version_command = (*execution_prefix, "--version")
    try:
        version_result = _run_spec_command(version_command, root, timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = "timed out" if isinstance(exc, subprocess.TimeoutExpired) else f"could not start: {type(exc).__name__}"
        return OpenSpecValidationEvidence(
            False,
            expected_version,
            None,
            safe_command,
            errors=(f"configured OpenSpec version command {reason}",),
            resolved_executable=resolved_executable,
        )
    version_text = (version_result.stdout or "") + "\n" + (version_result.stderr or "")
    observed = _openspec_version(version_text)
    version_errors: list[str] = []
    if version_result.returncode != 0:
        version_errors.append(f"configured OpenSpec version command exited {version_result.returncode}")
    if observed is None:
        version_errors.append("configured OpenSpec version output did not contain a semantic version")
    elif observed != expected_version:
        version_errors.append(
            f"configured OpenSpec version '{observed}' does not match exact pin '{expected_version}'"
        )
    if version_errors:
        return OpenSpecValidationEvidence(
            False,
            expected_version,
            observed,
            safe_command,
            exit_code=version_result.returncode,
            stdout_excerpt=_audit_excerpt(version_result.stdout),
            stderr_excerpt=_audit_excerpt(version_result.stderr),
            errors=tuple(version_errors),
            resolved_executable=resolved_executable,
        )

    validation_command = (*execution_prefix, "validate", "--all", "--strict", "--json", "--no-interactive")
    safe_validation_command = _safe_command(validation_command)
    try:
        result = _run_spec_command(validation_command, root, timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = "timed out" if isinstance(exc, subprocess.TimeoutExpired) else f"could not start: {type(exc).__name__}"
        return OpenSpecValidationEvidence(
            False,
            expected_version,
            observed,
            safe_command,
            safe_validation_command,
            errors=(f"configured OpenSpec validation {reason}",),
            resolved_executable=resolved_executable,
        )
    errors: list[str] = []
    missing_payload = object()
    payload: object = missing_payload
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        errors.append("configured OpenSpec validation did not emit one JSON document")
    if payload is not missing_payload and not isinstance(payload, dict):
        errors.append("configured OpenSpec validation JSON root must be an object")
    if result.returncode != 0:
        errors.append(f"configured OpenSpec validation exited {result.returncode}")
    sync_status = "unknown"
    if isinstance(payload, dict):
        declared_sync = payload.get("syncStatus", payload.get("sync_status"))
        if isinstance(declared_sync, str) and declared_sync.strip():
            sync_status = declared_sync.strip().casefold()
        if sync_status in {"out_of_sync", "out-of-sync", "stale", "failed", "required", "pending"}:
            errors.append(f"configured OpenSpec validation declared sync status '{sync_status}'")
    return OpenSpecValidationEvidence(
        not errors,
        expected_version,
        observed,
        safe_command,
        safe_validation_command,
        result.returncode,
        sync_status,
        _audit_excerpt(result.stdout),
        _audit_excerpt(result.stderr),
        tuple(errors),
        resolved_executable,
    )


def spec_audit_report(
    root: Path,
    adapter: SpecAdapter,
    *,
    project_revision: str,
    openspec_validation: OpenSpecValidationEvidence | None = None,
    risk_policy_fingerprint: str | None = None,
    risk_classes: tuple[str, ...] | list[str] = (),
) -> dict[str, object]:
    """Return a bounded, read-only structural inventory and audit decision."""
    root = Path(root).resolve()
    snapshot = adapter.discover(root)
    active_ids = {
        record.id
        for owner in snapshot.owners
        if owner.canonical
        for record in (owner, *owner.requirements)
    }
    blockers: list[str] = []
    for owner in snapshot.owners:
        if not owner.canonical:
            continue
        for record in (owner, *owner.requirements):
            for dependency in sorted(set(record.dependencies)):
                if dependency not in active_ids:
                    blockers.append(
                        f"active requirement '{_bounded(record.id)}' has unresolved reference '{_bounded(dependency)}'"
                    )
    review_findings = _duplicate_requirement_findings(snapshot)
    pending, pending_omitted = _pending_openspec_changes(root) if snapshot.adapter == "openspec" else ([], 0)
    if pending:
        review_findings.append(
            f"{len(pending) + pending_omitted} pending OpenSpec change(s) require owner review"
        )
    if openspec_validation is not None and not openspec_validation.ok:
        blockers.extend(openspec_validation.errors)
    owners = [
        {
            "id": _bounded(owner.id),
            "path": _bounded(owner.path),
            "status": owner.status,
            "requirement_ids": [_bounded(item.id) for item in owner.requirements[:_AUDIT_LIST_LIMIT]],
            "requirements_omitted": max(0, len(owner.requirements) - _AUDIT_LIST_LIMIT),
        }
        for owner in snapshot.owners[:_AUDIT_LIST_LIMIT]
    ]
    status = "blocked" if blockers else "review" if review_findings else "passed"
    if blockers:
        next_action = "resolve the listed structural or validator failures, then rerun spec-audit"
    elif review_findings:
        next_action = "review the labeled ambiguity or pending change; no structural blocker was proven"
    else:
        next_action = "no structural blocker found; continue normal independent review and behavioral validation"
    return {
        "schema_version": 1,
        "assessment": "structural-only",
        "read_only": True,
        "project_revision": _bounded(project_revision),
        "adapter": {"name": snapshot.adapter, "version": snapshot.adapter_version},
        "counts": {
            "active_owners": sum(1 for owner in snapshot.owners if owner.canonical),
            "historical_owners": sum(1 for owner in snapshot.owners if not owner.canonical),
            "pending_changes": len(pending) + pending_omitted,
        },
        "owners": owners,
        "owners_omitted": max(0, len(snapshot.owners) - _AUDIT_LIST_LIMIT),
        "pending_changes": pending,
        "pending_changes_omitted": pending_omitted,
        "blocking_findings": [_bounded(item) for item in blockers[:_AUDIT_LIST_LIMIT]],
        "blocking_findings_omitted": max(0, len(blockers) - _AUDIT_LIST_LIMIT),
        "review_findings": [_bounded(item) for item in review_findings[:_AUDIT_LIST_LIMIT]],
        "review_findings_omitted": max(0, len(review_findings) - _AUDIT_LIST_LIMIT),
        "openspec_validation": openspec_validation.to_dict() if openspec_validation is not None else None,
        "risk_policy": {
            "fingerprint": _bounded(risk_policy_fingerprint) if risk_policy_fingerprint else None,
            "classes": sorted({_bounded(item, 64) for item in risk_classes}),
        },
        "status": status,
        "next_action": next_action,
    }


def _run_spec_command(command: tuple[str, ...], root: Path, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
        }
    }
    environment.update({"CI": "1", "NO_COLOR": "1", "OPEN_SPEC_INTERACTIVE": "0"})
    return subprocess.run(
        command,
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        shell=False,
    )


def _resolve_spec_executable(root: Path, value: str) -> str | None:
    candidate = Path(value)
    if candidate.is_absolute() or candidate.parent != Path("."):
        path = candidate if candidate.is_absolute() else root / candidate
        return str(path.resolve()) if path.is_file() else None
    return shutil.which(value)


def _openspec_version(text: str) -> str | None:
    match = _VERSION_PATTERN.search(text[:_AUDIT_TEXT_LIMIT])
    return match.group(1) if match else None


def _audit_excerpt(value: str | None) -> str:
    text = redact_output(value or "")
    text = _AUDIT_SECRET_ASSIGNMENT.sub(r"\1\2***REDACTED***", text)
    return text[-_AUDIT_TEXT_LIMIT:]


def _safe_command(command: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    redact_next = False
    for item in command:
        if redact_next:
            result.append("***REDACTED***")
            redact_next = False
            continue
        lower = item.casefold()
        if any(marker in lower for marker in SECRET_MARKERS) and "=" not in item:
            result.append(_bounded(item))
            redact_next = True
        else:
            result.append(_bounded(redact_output(item)))
    return tuple(result)


def _bounded(value: object, limit: int = 300) -> str:
    text = str(value).replace("\x00", "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _decision_reference(value: object) -> str:
    reference = nonempty(value, "decision reference")
    if len(reference) > 500 or any(char in reference for char in ("\x00", "\r", "\n")):
        raise SpecAdapterError("decision reference must be bounded and single-line")
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise SpecAdapterError("decision URL must be HTTPS without embedded credentials")
        return reference
    path, separator, fragment = reference.partition("#")
    local_path(path)
    if separator and (not fragment or len(fragment) > 200 or any(char.isspace() for char in fragment)):
        raise SpecAdapterError("decision anchor must be non-empty, bounded and contain no whitespace")
    return reference


def _duplicate_requirement_findings(snapshot: SpecSnapshot) -> list[str]:
    by_text: dict[str, list[str]] = {}
    for owner in snapshot.owners:
        if not owner.canonical:
            continue
        for requirement in owner.requirements:
            normalized = " ".join(requirement.text.casefold().split())
            if normalized:
                by_text.setdefault(normalized, []).append(requirement.id)
    return [
        "suspected semantic overlap (exact normalized text only): " + ", ".join(_bounded(item) for item in sorted(ids))
        for ids in by_text.values()
        if len(ids) > 1
    ]


def _pending_openspec_changes(root: Path) -> tuple[list[str], int]:
    folder = root / "openspec" / "changes"
    if not folder.is_dir():
        return [], 0
    pending = sorted(
        item.relative_to(root).as_posix()
        for item in folder.iterdir()
        if item.is_dir() and item.name != "archive"
    )
    return pending[:_AUDIT_LIST_LIMIT], max(0, len(pending) - _AUDIT_LIST_LIMIT)


def _same_revision(recorded: Any, expected: str) -> bool:
    """The manifest revision must be the same immutable identity.

    Abbreviated prefixes are not accepted. The contract does not define a
    short form, and a shared prefix is not a unique object.
    """
    return isinstance(recorded, str) and recorded == expected


def _dependency_values(value: Any, label: str, errors: list[str]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        errors.append(f"{label} dependencies must be a list")
        return ()
    result: list[str] = []
    for index, dependency in enumerate(value):
        if not isinstance(dependency, str) or not dependency.strip():
            errors.append(f"{label} dependency #{index} must be a non-empty string")
        else:
            result.append(dependency)
    return tuple(result)


def _path_values(value: Any, label: str, errors: list[str]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        errors.append(f"{label} must be a list of paths")
        return ()
    result: list[str] = []
    for index, path in enumerate(value):
        if not isinstance(path, str) or not path.strip():
            errors.append(f"{label} path #{index} must be a non-empty string")
            continue
        try:
            result.append(local_path(path))
        except SpecAdapterError as exc:
            errors.append(f"{label} path #{index} is invalid: {exc}")
    return tuple(result)


def validate_prerequisite_manifest(
    manifest: Any,
    *,
    allowed_files: tuple[str, ...] | list[str] | None = None,
    expected_candidate_revision: str | None = None,
    expected_base_revision: str | None = None,
    expected_changed_files: tuple[str, ...] | list[str] | None = None,
    risk_policy: dict[str, dict[str, object]] | None = None,
    risk_policy_fingerprint: str | None = None,
    executed_gates: tuple[str, ...] | list[str] | None = None,
) -> SpecGovernanceVerdict:
    """Validate a prerequisite structural impact manifest fail-closed.

    Blocks missing owners/IDs, contradictory declared active statuses,
    missing affected references, and skipped required validation/sync.
    Ambiguous semantic overlap is recorded as a review finding, not a deterministic error.
    """
    errors: list[str] = []
    review_findings: list[str] = []

    if not isinstance(manifest, dict):
        return SpecGovernanceVerdict(ok=False, errors=("prerequisite manifest must be a JSON object",))

    if manifest.get("schema_version") != 1:
        return SpecGovernanceVerdict(
            ok=False,
            errors=(f"unsupported manifest schema_version: {manifest.get('schema_version')}",),
        )

    # Pinned adapter check
    adapter_info = manifest.get("adapter")
    if not isinstance(adapter_info, dict) or "name" not in adapter_info:
        errors.append("manifest must declare a configured pinned adapter")
    else:
        name = adapter_info.get("name")
        version = adapter_info.get("version")
        if not isinstance(name, str) or name not in PINNED_ADAPTERS:
            errors.append(f"adapter '{name}' is not a configured pinned adapter")
        elif not isinstance(version, str) or not version.strip() or version not in PINNED_ADAPTERS[name]:
            errors.append(
                f"adapter '{name}' (version {version!r}) is missing a valid configured version pin; allowed: {PINNED_ADAPTERS[name]}"
            )
        else:
            try:
                resolve_pinned_adapter(
                    name,
                    version,
                    contract_path=adapter_info.get("contract_path")
                    or manifest.get("contract_path")
                    or "adapter.json",
                )
            except SpecAdapterError as exc:
                errors.append(f"pinned adapter resolution failed: {exc}")

    # Required adapter validation and sync completion evidence
    val_status = manifest.get("validation_status")
    if val_status is None:
        errors.append("manifest missing required adapter validation evidence (validation_status)")
    elif val_status not in {"passed", "ok", "valid", "completed", "verified"}:
        errors.append(f"required pinned adapter validation was not passed: validation_status is '{val_status}'")
    if manifest.get("skipped_validation") is True:
        errors.append("skipped required pinned adapter validation")

    sync_status = manifest.get("sync_status")
    if sync_status is None:
        errors.append("manifest missing required spec sync evidence (sync_status)")
    elif sync_status not in {"in_sync", "synced", "passed", "ok", "completed", "verified"}:
        errors.append(f"required spec sync was not completed: sync_status is '{sync_status}'")
    if manifest.get("skipped_sync") is True:
        errors.append("skipped required spec sync")
    if manifest.get("sync_required") is True and not manifest.get("synced"):
        errors.append("required spec sync was not completed")

    # Candidate, baseline, and changed files binding
    cand_rev = manifest.get("candidate_revision")
    base_rev = manifest.get("base_revision")
    changed = manifest.get("changed_files")

    if expected_candidate_revision is not None:
        if not cand_rev:
            errors.append(f"manifest missing candidate_revision (expected '{expected_candidate_revision}')")
        elif not _same_revision(cand_rev, expected_candidate_revision):
            errors.append(
                f"manifest candidate_revision '{cand_rev}' does not match candidate '{expected_candidate_revision}'"
            )

    if expected_base_revision is not None:
        if not base_rev:
            errors.append(f"manifest missing base_revision (expected '{expected_base_revision}')")
        elif not _same_revision(base_rev, expected_base_revision):
            errors.append(
                f"manifest base_revision '{base_rev}' does not match baseline '{expected_base_revision}'"
            )

    if expected_changed_files is not None:
        if changed is None or not isinstance(changed, list):
            errors.append("manifest missing changed_files list")
        else:
            manifest_set = {f.replace("\\", "/").lstrip("/") for f in changed if isinstance(f, str)}
            expected_set = {f.replace("\\", "/").lstrip("/") for f in expected_changed_files if isinstance(f, str)}
            if manifest_set != expected_set:
                errors.append(
                    f"manifest changed_files {sorted(manifest_set)} does not match actual diff {sorted(expected_set)}"
                )

    # Owners and IDs check
    owners = manifest.get("owners")
    if not isinstance(owners, list):
        errors.append("manifest owners must be a list")
        owners = []

    known_owner_ids: set[str] = set()
    owner_statuses: dict[str, str] = {}
    owner_requirements: dict[str, set[str]] = {}
    known_record_ids: set[str] = set()
    record_dependencies: dict[int, tuple[str, ...]] = {}
    active_ids: set[str] = set()
    archived_markers = ("/archive/", "changes/archive", "openspec/changes/archive")

    for idx, owner in enumerate(owners):
        if not isinstance(owner, dict):
            errors.append(f"owner #{idx} must be an object")
            continue
        oid = owner.get("id")
        path = owner.get("path")
        status = owner.get("status")
        canonical = owner.get("canonical")

        if not isinstance(oid, str) or not oid.strip():
            errors.append(f"owner #{idx} has missing or empty id")
            continue
        if not isinstance(path, str) or not path.strip():
            errors.append(f"owner '{oid}' has missing or empty path")
            continue
        if oid in known_record_ids:
            errors.append(f"duplicate authoritative owner id '{oid}' at '{path}'")
        known_record_ids.add(oid)

        # Status validation and contradictory checks
        if status not in {"active", "history"}:
            errors.append(f"owner '{oid}' has unsupported or missing status: {status!r}")
        else:
            if oid in owner_statuses and owner_statuses[oid] != status:
                errors.append(
                    f"owner '{oid}' has contradictory declared active statuses ({owner_statuses[oid]} vs {status})"
                )
            owner_statuses[oid] = status

            is_active = (status == "active") and (canonical is not False)
            # Canonical vs status contradiction
            if canonical is not None:
                if canonical is True and status != "active":
                    errors.append(f"owner '{oid}' has contradictory status: canonical=True but status is '{status}'")
                elif canonical is False and status == "active":
                    errors.append(f"owner '{oid}' has contradictory status: canonical=False but status is 'active'")

            # Historical archive path declared active
            normalized_path = path.replace("\\", "/")
            if any(marker in normalized_path for marker in archived_markers) and status == "active":
                errors.append(f"owner '{oid}' at historical archive path '{path}' cannot have active status")

            if is_active:
                active_ids.add(oid)

        known_owner_ids.add(oid)
        record_dependencies[id(owner)] = _dependency_values(owner.get("dependencies", []), f"owner '{oid}'", errors)
        req_set: set[str] = set()
        owner_requirements[oid] = req_set

        # Requirement ID checks within owner
        reqs = owner.get("requirements", [])
        if not isinstance(reqs, list):
            errors.append(f"requirements for owner '{oid}' must be a list")
        else:
            for ridx, req in enumerate(reqs):
                if not isinstance(req, dict):
                    errors.append(f"requirement #{ridx} in owner '{oid}' must be an object")
                    continue
                rid = req.get("id")
                if not isinstance(rid, str) or not rid.strip():
                    errors.append(f"requirement #{ridx} in owner '{oid}' has missing or empty id")
                else:
                    if rid in known_record_ids:
                        errors.append(
                            f"duplicate authoritative requirement id '{rid}' in owner '{oid}' at '{path}'"
                        )
                    known_record_ids.add(rid)
                    req_set.add(rid)
                    if status == "active" and canonical is not False:
                        active_ids.add(rid)
                record_dependencies[id(req)] = _dependency_values(
                    req.get("dependencies", []), f"requirement '{rid}' in owner '{oid}'", errors
                )

    # Validate active dependencies against declared active IDs
    for owner in owners:
        if not isinstance(owner, dict):
            continue
        oid = owner.get("id")
        status = owner.get("status")
        canonical = owner.get("canonical")
        if status == "active" and canonical is not False:
            for dep in sorted(set(record_dependencies.get(id(owner), ()))):
                if dep not in active_ids:
                    errors.append(
                        f"missing affected reference: active owner '{oid}' has unresolved dependency '{dep}'"
                    )
            reqs = owner.get("requirements", [])
            if isinstance(reqs, list):
                for req in reqs:
                    if isinstance(req, dict):
                        rid = req.get("id")
                        for dep in sorted(set(record_dependencies.get(id(req), ()))):
                            if dep not in active_ids:
                                errors.append(
                                    f"missing affected reference: requirement '{rid}' in owner '{oid}' has unresolved dependency '{dep}'"
                                )

    # Requirements changes check against declared records
    req_changes = manifest.get("requirements", [])
    requirement_changed = False
    changed_records: list[dict[str, Any]] = []
    configured_risk_policy = risk_policy or {}
    if not isinstance(req_changes, list):
        errors.append("manifest requirements must be a list")
    else:
        if len(req_changes) > _MANIFEST_RECORD_LIMIT:
            errors.append(f"manifest requirements exceeds the {_MANIFEST_RECORD_LIMIT}-record limit")
        for cidx, change in enumerate(req_changes):
            if not isinstance(change, dict):
                errors.append(f"affected requirement #{cidx} must be an object")
                continue
            unknown_change_fields = set(change) - {
                "requirement_id", "id", "owner_id", "path", "change", "owner_role",
                "decision_refs", "superseded_by", "risk_class",
            }
            if unknown_change_fields:
                errors.append(
                    f"affected requirement #{cidx} has unsupported fields: "
                    + ", ".join(sorted(unknown_change_fields))
                )
            rid = change.get("requirement_id") or change.get("id")
            oid = change.get("owner_id")
            ctype = change.get("change")
            if ctype in {"added", "modified", "removed", "superseded"}:
                requirement_changed = True
                changed_records.append(change)
                owner_role = change.get("owner_role")
                if (
                    not isinstance(owner_role, str)
                    or not owner_role.strip()
                    or len(owner_role) > 100
                    or any(char in owner_role for char in ("\x00", "\r", "\n"))
                ):
                    errors.append(
                        f"affected requirement '{rid}' requires a bounded non-empty owner_role"
                    )
                decision_refs = change.get("decision_refs")
                if not isinstance(decision_refs, list) or not decision_refs or len(decision_refs) > 32:
                    errors.append(
                        f"affected requirement '{rid}' requires one to 32 approved decision references"
                    )
                else:
                    normalized_refs: list[str] = []
                    for ref_index, reference in enumerate(decision_refs):
                        try:
                            normalized_refs.append(_decision_reference(reference))
                        except SpecAdapterError as exc:
                            errors.append(
                                f"affected requirement '{rid}' decision reference #{ref_index} is invalid: {exc}"
                            )
                    if len(set(normalized_refs)) != len(normalized_refs):
                        errors.append(f"affected requirement '{rid}' decision_refs must not contain duplicates")
                if configured_risk_policy:
                    risk_class = change.get("risk_class")
                    if not isinstance(risk_class, str) or risk_class not in configured_risk_policy:
                        errors.append(
                            f"affected requirement '{rid}' must declare a configured risk_class"
                        )
            if not isinstance(rid, str) or not rid.strip():
                errors.append(f"affected requirement #{cidx} has missing or empty requirement_id")
                continue
            if not isinstance(oid, str) or not oid.strip():
                errors.append(f"affected requirement '{rid}' has missing or empty owner_id")
            elif oid not in known_owner_ids:
                errors.append(f"affected requirement '{rid}' references missing owner '{oid}'")
            else:
                if ctype not in {"removed", "superseded"} and rid not in owner_requirements.get(oid, set()):
                    errors.append(f"affected requirement '{rid}' not found in declared requirements for owner '{oid}'")
            if ctype == "superseded":
                replacement = change.get("superseded_by")
                if not isinstance(replacement, str) or not replacement.strip():
                    errors.append(f"superseded requirement '{rid}' requires superseded_by")
                elif replacement == rid:
                    errors.append(f"superseded requirement '{rid}' cannot supersede itself")
                elif replacement not in active_ids:
                    errors.append(
                        f"superseded requirement '{rid}' replacement '{replacement}' is not active"
                    )

    classification = manifest.get("classification_counts")
    expected_classes = {"added", "modified", "removed", "superseded", "unchanged"}
    if not isinstance(classification, dict) or set(classification) != expected_classes:
        errors.append("manifest classification_counts must contain added, modified, removed, superseded and unchanged")
    elif any(type(value) is not int or value < 0 for value in classification.values()):
        errors.append("manifest classification_counts values must be non-negative integers")
    unchanged_ids = manifest.get("unchanged_requirement_ids")
    if not isinstance(unchanged_ids, list) or not all(isinstance(item, str) and item for item in unchanged_ids):
        errors.append("manifest unchanged_requirement_ids must be a list of non-empty ids")
    omitted_unchanged = manifest.get("unchanged_requirements_omitted")
    if type(omitted_unchanged) is not int or omitted_unchanged < 0:
        errors.append("manifest unchanged_requirements_omitted must be a non-negative integer")
    if (
        isinstance(classification, dict)
        and set(classification) == expected_classes
        and all(type(value) is int and value >= 0 for value in classification.values())
        and isinstance(req_changes, list)
        and isinstance(unchanged_ids, list)
        and type(omitted_unchanged) is int
        and omitted_unchanged >= 0
    ):
        actual_counts = {
            label: sum(
                1 for change in req_changes
                if isinstance(change, dict) and change.get("change") == label
            )
            for label in ("added", "modified", "removed", "superseded")
        }
        actual_counts["unchanged"] = len(unchanged_ids) + omitted_unchanged
        if classification != actual_counts:
            errors.append("manifest classification_counts do not match the declared requirement classifications")
    if isinstance(unchanged_ids, list):
        if len(unchanged_ids) > _AUDIT_LIST_LIMIT:
            errors.append(f"manifest unchanged_requirement_ids exceeds the {_AUDIT_LIST_LIMIT}-item limit")
        if len({item for item in unchanged_ids if isinstance(item, str)}) != len(unchanged_ids):
            errors.append("manifest unchanged_requirement_ids must not contain duplicates")

    if requirement_changed and expected_candidate_revision is not None and configured_risk_policy:
        if not isinstance(risk_policy_fingerprint, str) or not risk_policy_fingerprint:
            errors.append("configured spec risk policy is missing its deterministic fingerprint")
        approvals = manifest.get("approval_evidence")
        if not isinstance(approvals, list):
            errors.append("requirement changes require exact-candidate approval_evidence")
            approvals = []
        elif len(approvals) > _APPROVAL_EVIDENCE_LIMIT:
            errors.append(
                f"approval_evidence exceeds the {_APPROVAL_EVIDENCE_LIMIT}-record limit"
            )
        valid_roles_by_requirement: dict[str, set[str]] = {}
        for index, evidence in enumerate(approvals):
            if not isinstance(evidence, dict):
                errors.append(f"approval_evidence #{index} must be an object")
                continue
            unknown_evidence_fields = set(evidence) - {
                "requirement_id", "actor", "role", "decision_ref",
                "candidate_revision", "policy_fingerprint",
            }
            if unknown_evidence_fields:
                errors.append(
                    f"approval_evidence #{index} has unsupported fields: "
                    + ", ".join(sorted(unknown_evidence_fields))
                )
            requirement_id = evidence.get("requirement_id")
            actor = evidence.get("actor")
            role = evidence.get("role")
            decision_ref = evidence.get("decision_ref")
            revision = evidence.get("candidate_revision")
            fingerprint = evidence.get("policy_fingerprint")
            change = next(
                (item for item in changed_records if item.get("requirement_id") == requirement_id),
                None,
            )
            if change is None:
                errors.append(f"approval_evidence #{index} references an unknown changed requirement")
                continue
            risk_class = change.get("risk_class")
            policy = configured_risk_policy.get(risk_class) if isinstance(risk_class, str) else None
            required_roles = policy.get("required_owner_roles", []) if isinstance(policy, dict) else []
            if not isinstance(actor, str) or not actor.strip() or len(actor) > 100 or any(
                char in actor for char in ("\x00", "\r", "\n")
            ):
                errors.append(f"approval_evidence #{index} requires a bounded single-line actor")
            if not isinstance(role, str) or role not in required_roles:
                errors.append(
                    f"approval_evidence #{index} role is not required by risk class '{risk_class}'"
                )
            try:
                normalized_ref = _decision_reference(decision_ref)
            except SpecAdapterError as exc:
                errors.append(f"approval_evidence #{index} decision_ref is invalid: {exc}")
                normalized_ref = None
            decision_refs = change.get("decision_refs")
            if normalized_ref is not None and (
                not isinstance(decision_refs, list) or normalized_ref not in decision_refs
            ):
                errors.append(
                    f"approval_evidence #{index} decision_ref is not declared for requirement '{requirement_id}'"
                )
            if revision != expected_candidate_revision or revision != cand_rev:
                errors.append(f"approval_evidence #{index} is not bound to the exact candidate revision")
            if fingerprint != risk_policy_fingerprint:
                errors.append(f"approval_evidence #{index} has a stale or mismatched policy fingerprint")
            if isinstance(role, str) and role in required_roles:
                valid_roles_by_requirement.setdefault(str(requirement_id), set()).add(role)

        required_gates: set[str] = set()
        for change in changed_records:
            requirement_id = str(change.get("requirement_id"))
            risk_class = change.get("risk_class")
            policy = configured_risk_policy.get(risk_class) if isinstance(risk_class, str) else None
            if not isinstance(policy, dict):
                continue
            required_roles = {
                role for role in policy.get("required_owner_roles", []) if isinstance(role, str)
            }
            missing_roles = sorted(required_roles - valid_roles_by_requirement.get(requirement_id, set()))
            for role in missing_roles:
                errors.append(
                    f"affected requirement '{requirement_id}' lacks exact-candidate approval from role '{role}'"
                )
            required_gates.update(
                gate for gate in policy.get("required_validation_gates", []) if isinstance(gate, str)
            )
        actual_gates = set(executed_gates or ())
        for gate in sorted(required_gates - actual_gates):
            errors.append(f"spec risk policy gate '{gate}' did not execute for the exact candidate")

    if requirement_changed and expected_candidate_revision is not None:
        behavioral = manifest.get("behavioral_evidence")
        if not isinstance(behavioral, list) or not behavioral:
            errors.append("requirement changes require behavioral_evidence bound to the candidate revision")
        else:
            passing = False
            for index, evidence in enumerate(behavioral):
                if not isinstance(evidence, dict):
                    errors.append(f"behavioral_evidence #{index} must be an object")
                    continue
                kind = evidence.get("kind")
                gate = evidence.get("gate")
                status = evidence.get("status")
                revision = evidence.get("candidate_revision")
                if not isinstance(kind, str) or not kind.strip():
                    errors.append(f"behavioral_evidence #{index} requires a non-empty kind")
                if not isinstance(gate, str) or not gate.strip():
                    errors.append(f"behavioral_evidence #{index} requires a non-empty executed gate name")
                if (
                    status in {"passed", "verified"}
                    and isinstance(revision, str)
                    and revision == cand_rev
                    and isinstance(gate, str)
                    and gate.strip()
                ):
                    passing = True
            if not passing:
                errors.append("requirement changes lack passing behavioral evidence for the exact candidate revision")

    # Missing affected references check
    unresolved = manifest.get("unresolved_references", [])
    if isinstance(unresolved, list):
        for u in unresolved:
            if isinstance(u, dict):
                sid = u.get("source_id")
                tgt = u.get("target")
                if sid and tgt and u.get("reason") == "no-active-target":
                    msg = f"missing affected reference: source '{sid}' cannot resolve target '{tgt}'"
                    if msg not in errors:
                        errors.append(msg)

    # Ambiguous semantic overlap is a review finding, NOT a deterministic error
    unmapped = _path_values(manifest.get("unmapped_files", []), "unmapped_files", errors)
    if unmapped:
        review_findings.append(f"ambiguous semantic overlap: unmapped files modified ({', '.join(sorted(unmapped))})")
    if manifest.get("semantic_overlap"):
        review_findings.append(f"ambiguous semantic overlap: {manifest.get('semantic_overlap')}")
    owner_decision = manifest.get("owner_decision_required")
    if isinstance(owner_decision, str) and owner_decision.strip():
        review_findings.append(f"unresolved owner decision: {_bounded(owner_decision, 500)}")
    waiver = manifest.get("waiver_request")
    if waiver is not None:
        if not isinstance(waiver, dict):
            errors.append("waiver_request must be an object with actor and reason")
        else:
            actor, reason = waiver.get("actor"), waiver.get("reason")
            if not isinstance(actor, str) or not actor.strip():
                errors.append("waiver_request requires a non-empty actor")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                errors.append("waiver_request reason must be non-empty and at most 500 characters")
            if isinstance(actor, str) and actor.strip() and isinstance(reason, str) and reason.strip() and len(reason) <= 500:
                review_findings.append(
                    f"waiver request recorded for review by {_bounded(actor)}: {_bounded(reason, 500)}"
                )

    return SpecGovernanceVerdict(
        ok=len(errors) == 0,
        errors=tuple(errors),
        review_findings=tuple(review_findings),
        manifest=manifest,
    )


def validate_manifest_discovery(
    manifest: dict[str, Any],
    before: SpecSnapshot,
    after: SpecSnapshot,
    changed_files: tuple[str, ...] | list[str],
    *,
    base_revision: str,
    candidate_revision: str,
) -> tuple[str, ...]:
    """Bind caller-supplied structural claims to the configured adapter's exact snapshots."""
    declared_changes = manifest.get("requirements", [])
    provenance: dict[str, dict[str, object]] = {}
    supersessions: dict[str, str] = {}
    if isinstance(declared_changes, list):
        for item in declared_changes:
            if not isinstance(item, dict) or not isinstance(item.get("requirement_id"), str):
                continue
            requirement_id = item["requirement_id"]
            extra = {
                key: value
                for key, value in item.items()
                if key not in {"requirement_id", "owner_id", "path", "change", "superseded_by"}
            }
            if extra:
                provenance[requirement_id] = extra
            if item.get("change") == "superseded" and isinstance(item.get("superseded_by"), str):
                supersessions[requirement_id] = item["superseded_by"]
    expected = structural_impact_manifest(
        before,
        after,
        changed_files,
        base_revision=base_revision,
        candidate_revision=candidate_revision,
        change_provenance=provenance,
        supersessions=supersessions,
    )
    errors: list[str] = []
    adapter = manifest.get("adapter")
    adapter_identity = (
        {"name": adapter.get("name"), "version": adapter.get("version")}
        if isinstance(adapter, dict)
        else adapter
    )
    if adapter_identity != expected["adapter"]:
        errors.append("manifest adapter does not match configured adapter discovery")
    for field in (
        "base_revision",
        "candidate_revision",
        "changed_files",
        "owners",
        "requirements",
        "classification_counts",
        "unchanged_requirement_ids",
        "unchanged_requirements_omitted",
        "unmapped_files",
        "unresolved_references",
    ):
        if manifest.get(field) != expected[field]:
            errors.append(f"manifest {field} does not match pinned adapter discovered snapshots")
    return tuple(errors)


def spec_governance_gate(
    before: SpecSnapshot | None = None,
    after: SpecSnapshot | None = None,
    changed_files: tuple[str, ...] | list[str] | None = None,
    *,
    prerequisite_manifest: dict[str, Any] | None = None,
    base_revision: str = "base",
    candidate_revision: str = "candidate",
    allowed_files: tuple[str, ...] | list[str] | None = None,
    expected_candidate_revision: str | None = None,
    expected_base_revision: str | None = None,
    expected_changed_files: tuple[str, ...] | list[str] | None = None,
    sync_status: str | None = None,
    validation_status: str | None = None,
    risk_policy: dict[str, dict[str, object]] | None = None,
    risk_policy_fingerprint: str | None = None,
    executed_gates: tuple[str, ...] | list[str] | None = None,
) -> SpecGovernanceVerdict:
    if prerequisite_manifest is not None:
        return validate_prerequisite_manifest(
            prerequisite_manifest,
            allowed_files=allowed_files,
            expected_candidate_revision=expected_candidate_revision,
            expected_base_revision=expected_base_revision,
            expected_changed_files=expected_changed_files,
            risk_policy=risk_policy,
            risk_policy_fingerprint=risk_policy_fingerprint,
            executed_gates=executed_gates,
        )
    if before is not None and after is not None:
        manifest = structural_impact_manifest(
            before,
            after,
            changed_files or (),
            base_revision=base_revision,
            candidate_revision=candidate_revision,
            sync_status=sync_status,
            validation_status=validation_status,
        )
        return validate_prerequisite_manifest(
            manifest,
            allowed_files=allowed_files,
            expected_candidate_revision=expected_candidate_revision,
            expected_base_revision=expected_base_revision,
            expected_changed_files=expected_changed_files,
            risk_policy=risk_policy,
            risk_policy_fingerprint=risk_policy_fingerprint,
            executed_gates=executed_gates,
        )
    return SpecGovernanceVerdict(ok=False, errors=("prerequisite manifest or before/after snapshots are required",))


def _read(root: Path, path: str) -> str:
    try:
        return project_file(root, path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SpecAdapterError(f"cannot read specification input: {path}") from exc


def _visible_lines(text: str) -> list[str]:
    lines: list[str] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(\x60{3,}|~{3,})", line)
        if fence:
            if (marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence)
                    and not line[marker.end():].strip()):
                fence = ""
            lines.append("\n")
        elif marker:
            fence = marker[1]
            lines.append("\n")
        else:
            lines.append(line)
    return lines


def _references(text: str, path: str) -> tuple[str, ...]:
    references: set[str] = set()
    for match in re.finditer(r"(?<!!)\[[^\]\n]*\]\(([^\s()]+)\)", text):
        target = unquote(match[1].strip("<>"))
        try:
            url = urlsplit(target)
        except ValueError:
            references.add(target)
            continue
        if url.scheme or url.netloc or url.query or target.startswith("/"):
            references.add(target)  # External or unsupported; never fetch it.
            continue
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), url.path))
        if not url.path:
            resolved = path
        references.add(resolved + ("#" + url.fragment if url.fragment else ""))
    return tuple(sorted(references))


class OpenSpecAdapter:
    """Pinned layout-v1 discovery without modifying any client file."""

    def discover(self, root: Path) -> SpecSnapshot:
        root = Path(root)
        owners: list[SpecOwner] = []
        for directory, pattern, status in (
            ("openspec/specs", "*/spec.md", "active"),
            ("openspec/changes/archive", "*/specs/*/spec.md", "history"),
        ):
            folder = project_file(root, directory)
            for file in sorted(folder.glob(pattern)):
                path = file.relative_to(root).as_posix()
                text = _read(root, path)
                original_lines = text.splitlines(keepends=True)
                lines = _visible_lines(text)
                starts: list[tuple[int, str]] = []
                for index, line in enumerate(lines):
                    match = re.match(r"^### Requirement: (.+?)\s*$", line)
                    if match:
                        title = match[1].strip()
                        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
                        if not slug:
                            raise SpecAdapterError(f"empty requirement anchor: {path}")
                        starts.append((index, path + "#requirement-" + slug))
                requirements: list[Requirement] = []
                owner_lines = lines.copy()
                for number, (start, identifier) in enumerate(starts):
                    end = starts[number + 1][0] if number + 1 < len(starts) else len(lines)
                    for index in range(start + 1, end):
                        if re.match(r"^#{1,3} ", lines[index]):
                            end = index
                            break
                    body = "".join(original_lines[start:end])
                    visible = "".join(lines[start:end])
                    owner_lines[start:end] = ["\n"] * (end - start)
                    requirements.append(Requirement(identifier, body, _references(visible, path)))
                owners.append(SpecOwner(path, path, status, tuple(requirements),
                                        _references("".join(owner_lines), path)))
        return SpecSnapshot("openspec", "layout-v1", tuple(sorted(owners, key=lambda o: o.id)))


def _object(value: object, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) - fields:
        raise SpecAdapterError(f"invalid {label} object or unsupported fields")
    return value


def _list(value: object, label: str) -> list:
    if not isinstance(value, list):
        raise SpecAdapterError(f"{label} must be a list")
    return value


class CustomSpecAdapter:
    """Consume a declarative export; never load project-local executable code."""

    def __init__(self, contract_path: str):
        self.contract_path = local_path(contract_path)

    def discover(self, root: Path) -> SpecSnapshot:
        try:
            data = json.loads(_read(Path(root), self.contract_path), object_pairs_hook=_unique_keys)
        except json.JSONDecodeError as exc:
            raise SpecAdapterError("custom adapter contract must be valid JSON") from exc
        data = _object(data, {"schema_version", "owners"}, "contract")
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise SpecAdapterError("unsupported custom adapter schema_version")
        owners = []
        for item in _list(data.get("owners"), "owners"):
            item = _object(item, {"id", "path", "status", "requirements", "dependencies"}, "owner")
            path = local_path(item.get("path"))
            project_file(Path(root), path)
            requirements = []
            for req in _list(item.get("requirements"), "requirements"):
                req = _object(req, {"id", "text", "dependencies"}, "requirement")
                requirements.append(Requirement(req.get("id"), req.get("text"),
                    tuple(_list(req.get("dependencies", []), "dependencies"))))
            owners.append(SpecOwner(item.get("id"), path, item.get("status"),
                tuple(requirements), tuple(_list(item.get("dependencies", []), "dependencies"))))
        snapshot = SpecSnapshot("custom-json", "1", tuple(owners), (self.contract_path,))
        return SpecSnapshot(snapshot.adapter, snapshot.adapter_version,
                            tuple(sorted(owners, key=lambda o: o.id)), snapshot.contract_paths)


def structural_impact_manifest(
    before: SpecSnapshot,
    after: SpecSnapshot,
    changed_files: list[str] | tuple[str, ...],
    *,
    base_revision: str,
    candidate_revision: str,
    sync_status: str | None = None,
    validation_status: str | None = None,
    behavioral_evidence: list[dict[str, object]] | None = None,
    change_provenance: dict[str, dict[str, object]] | None = None,
    supersessions: dict[str, str] | None = None,
) -> dict[str, object]:
    """Return JSON-ready structural evidence for explicitly supplied changed paths.

    Unmapped paths are visible rather than treated as proof of no impact.
    All post-change declared dependencies are checked, including unchanged owners
    referring to a removed target. History never satisfies an active reference.
    """
    nonempty(base_revision, "base_revision")
    nonempty(candidate_revision, "candidate_revision")
    if (before.adapter, before.adapter_version) != (after.adapter, after.adapter_version):
        raise SpecAdapterError("cannot compare different adapter contracts")
    if isinstance(changed_files, str):
        raise SpecAdapterError("changed_files must be a sequence of paths")
    paths = sorted({local_path(path) for path in changed_files})
    contract_paths = set(before.contract_paths + after.contract_paths)
    all_owners = before.owners + after.owners
    touched = {o.id for o in all_owners if o.path in paths or contract_paths.intersection(paths)}
    old = {r.id: (o, r) for o in before.owners for r in o.requirements}
    new = {r.id: (o, r) for o in after.owners for r in o.requirements}
    changes: list[dict[str, object]] = []
    unchanged: list[str] = []
    if change_provenance is not None and not isinstance(change_provenance, dict):
        raise SpecAdapterError("change_provenance must be an object")
    if supersessions is not None and not isinstance(supersessions, dict):
        raise SpecAdapterError("supersessions must be an object")
    provenance = change_provenance or {}
    explicit_supersessions = supersessions or {}
    if not all(isinstance(identifier, str) and identifier for identifier in provenance):
        raise SpecAdapterError("change_provenance keys must be non-empty requirement ids")
    if not all(
        isinstance(source, str) and source and isinstance(target, str) and target
        for source, target in explicit_supersessions.items()
    ):
        raise SpecAdapterError("supersessions must map non-empty requirement ids")
    provenance_fields = {"owner_role", "decision_refs", "risk_class"}
    for identifier in sorted(old.keys() | new.keys()):
        prior, current = old.get(identifier), new.get(identifier)
        if not any(pair and pair[0].id in touched for pair in (prior, current)):
            continue
        if prior is None:
            change = "added"
        elif current is None:
            change = "removed"
        elif (prior[1], prior[0].id, prior[0].path, prior[0].status) != (
                current[1], current[0].id, current[0].path, current[0].status):
            change = "modified"
        else:
            if current is not None and current[0].id in touched:
                unchanged.append(identifier)
            continue
        owner, _ = current or prior
        record: dict[str, object] = {
            "requirement_id": identifier,
            "owner_id": owner.id,
            "path": owner.path,
            "change": change,
        }
        if change == "removed" and identifier in explicit_supersessions:
            record["change"] = "superseded"
            record["superseded_by"] = explicit_supersessions[identifier]
        declared = provenance.get(identifier)
        if declared is not None:
            if not isinstance(declared, dict):
                raise SpecAdapterError(f"change provenance for {identifier} must be an object")
            unknown_fields = set(declared) - provenance_fields
            if unknown_fields:
                raise SpecAdapterError(
                    f"change provenance for {identifier} has unsupported fields: "
                    + ", ".join(sorted(str(field) for field in unknown_fields))
                )
            record.update(declared)
        changes.append(record)
    changed_ids = {str(item["requirement_id"]) for item in changes}
    stale_provenance = sorted(str(identifier) for identifier in set(provenance) - changed_ids)
    if stale_provenance:
        raise SpecAdapterError(
            "change provenance references unchanged or unknown requirements: "
            + ", ".join(stale_provenance)
        )
    removed_ids = {str(item["requirement_id"]) for item in changes if item["change"] in {"removed", "superseded"}}
    unknown_supersessions = sorted(set(explicit_supersessions) - removed_ids)
    if unknown_supersessions:
        raise SpecAdapterError(
            "supersession sources must be removed requirements: " + ", ".join(unknown_supersessions)
        )
    active_after = {requirement.id for owner in after.owners if owner.canonical for requirement in owner.requirements}
    for source, target in explicit_supersessions.items():
        nonempty(source, "supersession source")
        nonempty(target, "supersession target")
        if source == target or target not in active_after:
            raise SpecAdapterError(f"invalid supersession {source!r} -> {target!r}")
    active_ids = {record.id for owner in after.owners if owner.canonical
                  for record in (owner, *owner.requirements)}
    unresolved = []
    for owner in after.owners:
        for record in (owner, *owner.requirements):
            for target in sorted(set(record.dependencies)):
                if target not in active_ids:
                    unresolved.append({"source_id": record.id, "target": target,
                                       "reason": "no-active-target"})
    owners = {o.id: o for o in all_owners}
    manifest: dict[str, object] = {
        "schema_version": 1,
        "assessment": "structural-only",
        "adapter": {"name": after.adapter, "version": after.adapter_version},
        "base_revision": base_revision,
        "candidate_revision": candidate_revision,
        "changed_files": paths,
        "owners": [_owner_record(owners[key]) for key in sorted(touched)],
        "requirements": changes,
        "classification_counts": {
            label: sum(1 for item in changes if item["change"] == label)
            for label in ("added", "modified", "removed", "superseded")
        }
        | {"unchanged": len(unchanged)},
        "unchanged_requirement_ids": unchanged[:_AUDIT_LIST_LIMIT],
        "unchanged_requirements_omitted": max(0, len(unchanged) - _AUDIT_LIST_LIMIT),
        "unmapped_files": sorted(set(paths) - {o.path for o in all_owners} - contract_paths),
        "unresolved_references": sorted(unresolved, key=lambda r: (r["source_id"], r["target"])),
    }
    if changes:
        manifest["behavioral_evidence"] = list(behavioral_evidence or [])
    if sync_status is not None:
        manifest["sync_status"] = sync_status
    if validation_status is not None:
        manifest["validation_status"] = validation_status
    return manifest


def _owner_record(owner: SpecOwner) -> dict[str, object]:
    return {
        "id": owner.id, "path": owner.path, "status": owner.status,
        "canonical": owner.canonical, "dependencies": list(owner.dependencies),
        "requirements": [{"id": req.id, "text": req.text,
                          "dependencies": list(req.dependencies)} for req in owner.requirements],
    }


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SpecAdapterError(f"duplicate contract field: {key}")
        result[key] = value
    return result
