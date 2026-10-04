"""Deterministic file scope for auto-planned contracts.

A project may ship `stagemesh.scope.json` (or `.stagemesh/scope-map.json`): task areas, each with the labels and keywords that
select it and the narrow `allowed_files` a task of that area may touch. Auto-planning matches a task's labels, then its title,
then its description against the areas (the same order the project profile uses) and writes the matching scope into the contract
instead of `**`. Nothing here knows any particular project: the map is data the project supplies.

    {"schema_version": 1,
     "areas": [{"id": "docs", "labels": ["documentation"], "keywords": ["readme"], "allowed_files": ["README.md", "docs/**"],
                "exclusive": true}]}

When an area cannot be derived (no map, nothing matched, or the task spans too many areas) the caller decides what a broad scope
means; this module only reports that no bounded scope exists.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCOPE_MAP_FILES = ("stagemesh.scope.json", ".stagemesh/scope-map.json")
MAX_AREAS_PER_TASK = 2  # a task touching more areas than this is not narrow: it is ambiguous
BROAD_PATTERNS = frozenset({"**", "*", "**/*"})


class ScopeMapError(ValueError):
    pass


@dataclass(frozen=True)
class Area:
    id: str
    labels: tuple[str, ...]
    keywords: tuple[str, ...]
    allowed_files: tuple[str, ...]
    exclusive: bool = False  # when it matches, it alone decides (e.g. a docs task that merely mentions code stays docs-only)


@dataclass(frozen=True)
class ScopeDecision:
    mode: str  # "narrow" | "broad"
    allowed_files: tuple[str, ...]
    areas: tuple[str, ...] = ()
    source: str = ""  # label | title keyword | description keyword
    evidence: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "allowed_files": list(self.allowed_files),
            "areas": list(self.areas),
            "source": self.source,
            "evidence": list(self.evidence),
            "reason": self.reason,
            "summary": self.describe(),
        }

    def describe(self) -> str:
        if self.mode == "broad":
            return f"BROAD ({', '.join(self.allowed_files)}): {self.reason}"
        return f"narrow, area {'+'.join(self.areas)} via {self.source} ({'; '.join(self.evidence)}): {', '.join(self.allowed_files)}"


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ScopeMapError(f"{field} must be a list of non-empty strings")
    return tuple(v.strip() for v in value)


def load_scope_map(project: Path) -> tuple[Area, ...] | None:
    """None when the project has no scope map; ScopeMapError when it has one that is unusable."""
    for name in SCOPE_MAP_FILES:
        path = Path(project) / name
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ScopeMapError(f"{name} must be valid JSON: {exc}") from exc
            return parse_scope_map(raw, name)
    return None


def parse_scope_map(raw: Any, name: str = "scope map") -> tuple[Area, ...]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1 or not isinstance(raw.get("areas"), list):
        raise ScopeMapError(f"{name} must be an object with schema_version 1 and an areas list")
    areas: list[Area] = []
    for item in raw["areas"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ScopeMapError(f"{name}: every area needs an id")
        allowed = _strings(item.get("allowed_files"), f"{name}: area {item['id']} allowed_files")
        if not allowed:
            raise ScopeMapError(f"{name}: area {item['id']} needs allowed_files")
        if any(p.strip().replace("\\", "/") in BROAD_PATTERNS for p in allowed):
            raise ScopeMapError(f"{name}: area {item['id']} must be narrower than the whole repository")
        labels = _strings(item.get("labels"), f"{name}: area {item['id']} labels")
        keywords = _strings(item.get("keywords"), f"{name}: area {item['id']} keywords")
        if not labels and not keywords:
            raise ScopeMapError(f"{name}: area {item['id']} needs labels or keywords to be selectable")
        exclusive = item.get("exclusive", False)
        if not isinstance(exclusive, bool):
            raise ScopeMapError(f"{name}: area {item['id']} exclusive must be a boolean")
        areas.append(Area(item["id"].strip(), labels, keywords, allowed, exclusive))
    if len({a.id for a in areas}) != len(areas):
        raise ScopeMapError(f"{name}: area ids must be unique")
    return tuple(areas)


def _match_labels(labels: tuple[str, ...], wanted: tuple[str, ...]) -> list[str]:
    folded = {label.casefold() for label in labels}
    return [f"label {w}" for w in wanted if w.casefold() in folded]


def _match_keywords(text: str, wanted: tuple[str, ...]) -> list[str]:
    return [w for w in wanted if re.search(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", text, re.IGNORECASE)]


def derive_scope(areas: tuple[Area, ...] | None, labels: tuple[str, ...], title: str, body: str) -> ScopeDecision:
    """The narrow scope for a task, or a decision with mode "broad" and the reason no bounded scope could be derived."""
    if areas is None:
        return ScopeDecision("broad", ("**",), reason="the project has no scope map (stagemesh.scope.json)")
    for source, hit in (
        ("label", lambda a: _match_labels(labels, a.labels)),
        ("title keyword", lambda a: _match_keywords(title, a.keywords)),
        ("description keyword", lambda a: _match_keywords(body, a.keywords)),
    ):
        hits = {a.id: found for a in areas if (found := hit(a))}
        if not hits:
            continue
        exclusive = {a.id: hits[a.id] for a in areas if a.exclusive and a.id in hits}
        if exclusive:
            hits = exclusive
        if len(hits) > MAX_AREAS_PER_TASK:
            return ScopeDecision(
                "broad", ("**",), source=source, evidence=tuple(f"{k}: {', '.join(v)}" for k, v in hits.items()),
                reason=f"the task matches {len(hits)} areas by {source} ({', '.join(hits)}); too many to bound",
            )
        chosen = [a for a in areas if a.id in hits]
        files = tuple(dict.fromkeys(f for a in chosen for f in a.allowed_files))
        return ScopeDecision("narrow", files, tuple(hits), source, tuple(f"{k}: {', '.join(v)}" for k, v in hits.items()))
    return ScopeDecision("broad", ("**",), reason="no label or keyword of the task matches an area in the scope map")
