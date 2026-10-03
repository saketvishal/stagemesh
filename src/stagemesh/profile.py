"""Project profiles: per-project task types, validation gates and contract defaults.

A profile (`.stagemesh/profile.json`) lets StageMesh auto-plan contracts for a project whose tests cannot be guessed from root
files. It is provider neutral: it names gates, file scopes and labels, never agents. See docs/profiles.md.

Selection of a task type is deterministic: explicit labels win; otherwise keywords in the issue title; otherwise keywords in its
description. One product type -> that type; two or more product types -> the escalation type (full regression); only prep
types -> the prep type; nothing -> the default type. Evidence for the decision is returned and written into the contract.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .validation_plan import _is_broad_command, _is_broad_gate

PROFILE_FILE = Path(".stagemesh") / "profile.json"
LEVELS = {"light": "DOCS_ONLY", "standard": "LOCALIZED_CODE", "full": "CORE_LIFECYCLE_OR_SCHEMA_SECURITY"}
DOC_GATE_TOKENS = ("docs", "static", "lint", "spell")  # validation planning only runs these gates for DOCS_ONLY work
_VARIABLE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
_TYPE_KEYS = {
    "summary", "labels", "keywords", "allowed_files", "forbidden_files", "protected_files",
    "max_changed_files", "max_diff_lines", "validation_level", "gates",
}


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class TaskType:
    id: str
    summary: str
    labels: tuple[str, ...]
    keywords: tuple[str, ...]
    allowed_files: tuple[str, ...]
    forbidden_files: tuple[str, ...]
    protected_files: tuple[str, ...]
    max_changed_files: int
    max_diff_lines: int
    level: str
    gates: tuple[str, ...]


@dataclass(frozen=True)
class Profile:
    name: str
    variables: dict[str, Any]
    env_sets: dict[str, dict[str, str]]
    gates: dict[str, dict[str, Any]]
    types: dict[str, TaskType]
    forbidden_files: tuple[str, ...]
    default_type: str
    escalation_type: str
    escalate_at: int
    task_selection: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TypeDecision:
    type_id: str
    source: str  # label | title keyword | description keyword | escalation | default
    evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type_id, "source": self.source, "evidence": list(self.evidence)}


def profile_path(project: Path) -> Path:
    return Path(project) / PROFILE_FILE


def load_profile(project: Path) -> Profile | None:
    """None when the project has no profile; ProfileError when it has one that is not usable."""
    path = profile_path(project)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError(f"{PROFILE_FILE} must be valid JSON: {exc}") from exc
    return parse_profile(raw)


def parse_profile(raw: Any) -> Profile:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ProfileError("profile must be an object with schema_version 1")
    unknown = set(raw) - {
        "schema_version", "name", "variables", "env_sets", "gates", "forbidden_files", "defaults",
        "task_types", "type_selection", "task_selection",
    }
    if unknown:
        raise ProfileError(f"profile has unsupported keys: {', '.join(sorted(unknown))}")
    name = _text(raw.get("name"), "name")
    defaults = raw.get("defaults") or {}
    gates = raw.get("gates")
    if not isinstance(gates, dict) or not gates:
        raise ProfileError("profile gates must be a non-empty object")
    types: dict[str, TaskType] = {}
    for type_id, spec in (raw.get("task_types") or {}).items():
        types[type_id] = _task_type(type_id, spec, defaults, set(gates))
    if not types:
        raise ProfileError("profile task_types must not be empty")
    selection = raw.get("type_selection") or {}
    default_type = selection.get("default_type")
    escalation_type = selection.get("escalation_type")
    if default_type not in types or escalation_type not in types:
        raise ProfileError("type_selection.default_type and escalation_type must name task types")
    escalate_at = selection.get("escalate_at_product_types", 2)
    if isinstance(escalate_at, bool) or not isinstance(escalate_at, int) or escalate_at < 2:
        raise ProfileError("type_selection.escalate_at_product_types must be an integer >= 2")
    env_sets = raw.get("env_sets") or {}
    for set_name, values in env_sets.items():
        if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items()):
            raise ProfileError(f"env_sets.{set_name} must be an object of strings")
    profile = Profile(
        name=name,
        variables=dict(raw.get("variables") or {}),
        env_sets=env_sets,
        gates=gates,
        types=types,
        forbidden_files=_texts(raw.get("forbidden_files"), "forbidden_files"),
        default_type=default_type,
        escalation_type=escalation_type,
        escalate_at=escalate_at,
        task_selection=dict(raw.get("task_selection") or {}),
    )
    validate_profile(profile)
    return profile


def _task_type(type_id: str, spec: Any, defaults: dict[str, Any], gate_ids: set[str]) -> TaskType:
    if not isinstance(spec, dict):
        raise ProfileError(f"task type {type_id} must be an object")
    unknown = set(spec) - _TYPE_KEYS
    if unknown:
        raise ProfileError(f"task type {type_id} has unsupported keys: {', '.join(sorted(unknown))}")
    level = spec.get("validation_level")
    if level not in LEVELS:
        raise ProfileError(f"task type {type_id} validation_level must be one of: {', '.join(LEVELS)}")
    gates = _texts(spec.get("gates"), f"{type_id}.gates")
    missing = [g for g in gates if g not in gate_ids]
    if not gates or missing:
        raise ProfileError(f"task type {type_id} gates must be a non-empty list of known gates (unknown: {', '.join(missing) or 'none'})")
    allowed = _texts(spec.get("allowed_files"), f"{type_id}.allowed_files")
    if not allowed:
        raise ProfileError(f"task type {type_id} must define allowed_files")
    return TaskType(
        id=type_id,
        summary=str(spec.get("summary", "")),
        labels=_texts(spec.get("labels"), f"{type_id}.labels"),
        keywords=_texts(spec.get("keywords"), f"{type_id}.keywords"),
        allowed_files=allowed,
        forbidden_files=_texts(spec.get("forbidden_files"), f"{type_id}.forbidden_files"),
        protected_files=_texts(spec.get("protected_files"), f"{type_id}.protected_files"),
        max_changed_files=_positive(spec.get("max_changed_files", defaults.get("max_changed_files")), f"{type_id}.max_changed_files"),
        max_diff_lines=_positive(spec.get("max_diff_lines", defaults.get("max_diff_lines")), f"{type_id}.max_diff_lines"),
        level=level,
        gates=gates,
    )


def validate_profile(profile: Profile) -> None:
    """Fail at load time on anything that would make a generated contract unrunnable or mis-planned."""
    for type_id, task_type in profile.types.items():
        names = [expand_gate(profile, gate_id)["name"] for gate_id in task_type.gates]
        if len(set(names)) != len(names):
            raise ProfileError(f"task type {type_id} has duplicate gate names")
        commands = [expand_gate(profile, gate_id)["command"] for gate_id in task_type.gates]
        broad = [_is_broad_gate(n) or _is_broad_command(tuple(c)) for n, c in zip(names, commands)]
        if task_type.level == "light" and not all(any(t in n.casefold() for t in DOC_GATE_TOKENS) and not b for n, b in zip(names, broad)):
            raise ProfileError(
                f"light task type {type_id}: every gate name must contain one of {', '.join(DOC_GATE_TOKENS)} and not be broad, "
                "otherwise validation planning would skip it"
            )
        if task_type.level == "standard" and any(broad):
            raise ProfileError(f"standard task type {type_id}: gates must not be broad (planning would skip them)")
        if task_type.level == "full" and not any(broad):
            raise ProfileError(f"full task type {type_id} needs at least one broad gate (a name containing 'acceptance')")
        # A contract built for this type must parse and fit the store limit.
        from .auto_plan import validate_generated  # local import: auto_plan imports this module

        try:
            validate_generated(build_contract(profile, TypeDecision(type_id, "validation", ()), "probe", "probe-task", ""))
        except Exception as exc:  # noqa: BLE001 - surfaced as a profile problem
            raise ProfileError(f"task type {type_id} produces an invalid contract: {exc}") from exc


def resolve_variables(profile: Profile) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in profile.variables.items():
        if isinstance(value, dict):
            value = value.get("win32" if os.name == "nt" else "posix", value.get("default"))
        if not isinstance(value, str):
            raise ProfileError(f"variable {key} must be a string (or an object with win32/posix/default strings)")
        values[key] = os.environ.get(f"STAGEMESH_PROFILE_{key}", value)
    return values


def _expand(text: str, variables: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group(1) not in variables:
            raise ProfileError(f"unknown profile variable ${{{match.group(1)}}}")
        return variables[match.group(1)]

    return _VARIABLE.sub(replace, text)


def expand_gate(profile: Profile, gate_id: str) -> dict[str, Any]:
    spec = profile.gates[gate_id]
    unknown = set(spec) - {"name", "command", "cwd", "env", "timeout_seconds"}
    if unknown or not isinstance(spec.get("command"), list) or not spec["command"]:
        raise ProfileError(f"gate {gate_id} needs a command list and may only use name/command/cwd/env/timeout_seconds")
    variables = resolve_variables(profile)
    gate: dict[str, Any] = {
        "name": str(spec.get("name") or gate_id),
        "command": [_expand(str(part), variables) for part in spec["command"]],
        "timeout_seconds": spec.get("timeout_seconds", 600),
    }
    if spec.get("cwd"):
        gate["cwd"] = _expand(str(spec["cwd"]), variables)
    env_set = spec.get("env")
    if env_set is not None:
        if env_set not in profile.env_sets:
            raise ProfileError(f"gate {gate_id} references unknown env set {env_set}")
        gate["env"] = {k: _expand(v, variables) for k, v in profile.env_sets[env_set].items()}
    return gate


def _match_labels(labels: tuple[str, ...], wanted: tuple[str, ...]) -> list[str]:
    folded = {label.casefold() for label in labels}
    return [w for w in wanted if w.casefold() in folded]


def _match_keywords(text: str, wanted: tuple[str, ...]) -> list[str]:
    return [w for w in wanted if re.search(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", text, re.IGNORECASE)]


def resolve_type(profile: Profile, labels: tuple[str, ...], title: str, body: str) -> TypeDecision:
    escalation = profile.types[profile.escalation_type]
    forced = _match_labels(labels, escalation.labels)
    if forced:
        return TypeDecision(escalation.id, "label", tuple(f"label {w}" for w in forced))
    candidates = [t for t in profile.types.values() if t.id != escalation.id]
    for source, hit in (
        ("label", lambda t: _match_labels(labels, t.labels)),
        ("title keyword", lambda t: _match_keywords(title, t.keywords)),
        ("description keyword", lambda t: _match_keywords(body, t.keywords)),
    ):
        hits = {t.id: hit(t) for t in candidates if hit(t)}
        if not hits:
            continue
        evidence = tuple(f"{tid}: {', '.join(found)}" for tid, found in hits.items())
        product = [tid for tid in hits if profile.types[tid].level != "light"]
        if len(product) >= profile.escalate_at:
            return TypeDecision(escalation.id, "escalation", (f"{len(product)} product areas matched ({source})", *evidence))
        chosen = product[0] if product else next(iter(hits))
        return TypeDecision(chosen, source, evidence)
    default = profile.types[profile.default_type]
    return TypeDecision(default.id, "default", ("no label or keyword matched a task type",))


def build_contract(profile: Profile, decision: TypeDecision, objective: str, task_id: str, generated_by: str) -> dict[str, Any]:
    task_type = profile.types[decision.type_id]
    return {
        "objective": objective,
        "explicit": True,
        "validation_classification": LEVELS[task_type.level],
        "validation_escalation_reasons": [f"profile {profile.name}: task type {task_type.id} ({task_type.level} validation)"],
        "acceptance_criteria": [
            "The change fulfils the task objective and nothing else.",
            f"Every {task_type.id} validation gate of the {profile.name} profile passes.",
        ],
        "allowed_files": list(task_type.allowed_files),
        "forbidden_files": list(dict.fromkeys((*profile.forbidden_files, *task_type.forbidden_files))),
        "protected_files": list(task_type.protected_files),
        "required_tests": [expand_gate(profile, gate_id) for gate_id in task_type.gates],
        "max_changed_files": task_type.max_changed_files,
        "max_diff_lines": task_type.max_diff_lines,
        "generated_by": generated_by,
        "source_task": task_id,
        "profile": {"name": profile.name, "task_type": task_type.id, "validation_level": task_type.level, "selection": decision.to_dict()},
    }


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{field_name} must be a non-empty string")
    return value.strip()


def _texts(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ProfileError(f"{field_name} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _positive(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProfileError(f"{field_name} must be a positive integer (set it on the type or under defaults)")
    return value

