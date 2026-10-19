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
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .spec_refs import (
    Requirement, SpecAdapterError, SpecOwner, SpecSnapshot, local_path, nonempty, project_file,
)


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
