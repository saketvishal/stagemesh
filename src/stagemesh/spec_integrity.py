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
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .spec_refs import (
    Requirement, SpecAdapter, SpecAdapterError, SpecOwner, SpecSnapshot, local_path, nonempty, project_file,
)

PINNED_ADAPTERS: dict[str, tuple[str, ...]] = {
    "openspec": ("layout-v1",),
    "custom-json": ("1",),
}


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


def validate_prerequisite_manifest(
    manifest: Any,
    *,
    allowed_files: tuple[str, ...] | list[str] | None = None,
    expected_candidate_revision: str | None = None,
    expected_base_revision: str | None = None,
    expected_changed_files: tuple[str, ...] | list[str] | None = None,
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
        elif not (
            cand_rev == expected_candidate_revision
            or expected_candidate_revision.startswith(str(cand_rev))
            or str(cand_rev).startswith(expected_candidate_revision)
        ):
            errors.append(
                f"manifest candidate_revision '{cand_rev}' does not match candidate '{expected_candidate_revision}'"
            )

    if expected_base_revision is not None:
        if not base_rev:
            errors.append(f"manifest missing base_revision (expected '{expected_base_revision}')")
        elif not (
            base_rev == expected_base_revision
            or expected_base_revision.startswith(str(base_rev))
            or str(base_rev).startswith(expected_base_revision)
        ):
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
                    req_set.add(rid)
                    if status == "active" and canonical is not False:
                        active_ids.add(rid)

    # Validate active dependencies against declared active IDs
    for owner in owners:
        if not isinstance(owner, dict):
            continue
        oid = owner.get("id")
        status = owner.get("status")
        canonical = owner.get("canonical")
        if status == "active" and canonical is not False:
            deps = owner.get("dependencies", [])
            if isinstance(deps, (list, tuple)):
                for dep in sorted(set(deps)):
                    if dep not in active_ids:
                        errors.append(
                            f"missing affected reference: active owner '{oid}' has unresolved dependency '{dep}'"
                        )
            reqs = owner.get("requirements", [])
            if isinstance(reqs, list):
                for req in reqs:
                    if isinstance(req, dict):
                        rid = req.get("id")
                        rdeps = req.get("dependencies", [])
                        if isinstance(rdeps, (list, tuple)):
                            for dep in sorted(set(rdeps)):
                                if dep not in active_ids:
                                    errors.append(
                                        f"missing affected reference: requirement '{rid}' in owner '{oid}' has unresolved dependency '{dep}'"
                                    )

    # Requirements changes check against declared records
    req_changes = manifest.get("requirements", [])
    if isinstance(req_changes, list):
        for cidx, change in enumerate(req_changes):
            if not isinstance(change, dict):
                continue
            rid = change.get("requirement_id") or change.get("id")
            oid = change.get("owner_id")
            ctype = change.get("change")
            if not isinstance(rid, str) or not rid.strip():
                errors.append(f"affected requirement #{cidx} has missing or empty requirement_id")
                continue
            if not isinstance(oid, str) or not oid.strip():
                errors.append(f"affected requirement '{rid}' has missing or empty owner_id")
            elif oid not in known_owner_ids:
                errors.append(f"affected requirement '{rid}' references missing owner '{oid}'")
            else:
                if ctype != "removed" and rid not in owner_requirements.get(oid, set()):
                    errors.append(f"affected requirement '{rid}' not found in declared requirements for owner '{oid}'")

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
    unmapped = manifest.get("unmapped_files", [])
    if isinstance(unmapped, (list, tuple)) and unmapped:
        review_findings.append(f"ambiguous semantic overlap: unmapped files modified ({', '.join(sorted(unmapped))})")
    if manifest.get("semantic_overlap"):
        review_findings.append(f"ambiguous semantic overlap: {manifest.get('semantic_overlap')}")

    return SpecGovernanceVerdict(
        ok=len(errors) == 0,
        errors=tuple(errors),
        review_findings=tuple(review_findings),
        manifest=manifest,
    )


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
) -> SpecGovernanceVerdict:
    if prerequisite_manifest is not None:
        return validate_prerequisite_manifest(
            prerequisite_manifest,
            allowed_files=allowed_files,
            expected_candidate_revision=expected_candidate_revision,
            expected_base_revision=expected_base_revision,
            expected_changed_files=expected_changed_files,
        )
    if before is not None and after is not None:
        manifest = structural_impact_manifest(
            before,
            after,
            changed_files or (),
            base_revision=base_revision,
            candidate_revision=candidate_revision,
        )
        return validate_prerequisite_manifest(
            manifest,
            allowed_files=allowed_files,
            expected_candidate_revision=expected_candidate_revision,
            expected_base_revision=expected_base_revision,
            expected_changed_files=expected_changed_files,
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
    changes = []
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
            continue
        owner, _ = current or prior
        changes.append({"requirement_id": identifier, "owner_id": owner.id,
                        "path": owner.path, "change": change})
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
    return {
        "schema_version": 1,
        "assessment": "structural-only",
        "adapter": {"name": after.adapter, "version": after.adapter_version},
        "base_revision": base_revision,
        "candidate_revision": candidate_revision,
        "changed_files": paths,
        "owners": [_owner_record(owners[key]) for key in sorted(touched)],
        "requirements": changes,
        "unmapped_files": sorted(set(paths) - {o.path for o in all_owners} - contract_paths),
        "unresolved_references": sorted(unresolved, key=lambda r: (r["source_id"], r["target"])),
        "sync_status": "in_sync",
        "validation_status": "passed",
    }


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
