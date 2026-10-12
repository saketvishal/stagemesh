"""Default project profile created by `stagemesh init` when a project has none.

It uses the normal profile schema (see profile.py) and only conventions StageMesh already knows: the gates `detect_gates` finds at the
project root. When it finds none, the single gate is `git diff --check`, which is safe everywhere and proves nothing about behaviour;
the task type's summary says so. An existing profile, valid or not, is never touched.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from .auto_plan import FORBIDDEN_FILES as AUTO_PLAN_FORBIDDEN
from .auto_plan import detect_gates
from .profile import PROFILE_FILE, ProfileError, load_profile, parse_profile, profile_path

# The planner's own forbidden set (auto_plan), plus local databases, environments and caches.
FORBIDDEN_FILES = (*AUTO_PLAN_FORBIDDEN, "*.sqlite", "*.sqlite3", "*.db", ".venv/**", "**/node_modules/**", "**/__pycache__/**")
PLACEHOLDER_GATE = "default-acceptance-diff-check"  # project-smoke warns while this is the only gate
_FALLBACK_GATE = {"name": PLACEHOLDER_GATE, "command": ["git", "diff", "--check"], "timeout_seconds": 120}


def _gate_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_") or "gate"


def _argv(gate: dict[str, Any]) -> dict[str, Any]:
    """detect_gates wraps `npm test` in a shell; a profile gate must be a plain argv list that project-smoke accepts."""
    command = gate["command"]
    if command[0] in ("cmd", "sh") and len(command) > 2:
        return {**gate, "command": shlex.split(command[-1])}
    return gate


def is_placeholder(profile: Any) -> bool:
    """True while the generated `git diff --check` gate is the profile's only gate."""
    return {_gate_id(PLACEHOLDER_GATE)} == set(profile.gates)


def default_profile(project: Path) -> dict[str, Any]:
    detected = [_argv(g) for g in detect_gates(project)]
    gates = list(detected)
    if not any("acceptance" in g["name"].casefold() for g in gates):
        gates.append(dict(_FALLBACK_GATE))  # a `full` task type needs a broad gate; detected gates (e.g. a narrow smoke) may not be one
    ids = {_gate_id(g["name"]): g for g in gates}
    summary = (
        "Generated default: runs the gates detected at the project root."
        if detected
        else "Generated default: no test gate was detected, so this only runs `git diff --check`; add real gates to .stagemesh/profile.json."
    )
    return {
        "schema_version": 1,
        "name": re.sub(r"[^A-Za-z0-9._-]+", "-", project.name) or "project",
        "forbidden_files": list(FORBIDDEN_FILES),
        "defaults": {"max_changed_files": 60, "max_diff_lines": 6000},
        "gates": {gid: {"name": g["name"], "command": g["command"], "timeout_seconds": g["timeout_seconds"]} for gid, g in ids.items()},
        "task_types": {"default": {"summary": summary, "allowed_files": ["**"], "validation_level": "full", "gates": list(ids)}},
        "type_selection": {"default_type": "default", "escalation_type": "default"},
    }


def ensure_profile(project: Path) -> tuple[str, str]:
    """('kept' | 'created' | 'invalid', detail). Never overwrites or repairs an existing profile file."""
    path = profile_path(project)
    if path.exists():
        if not path.is_file():
            return "invalid", f"{PROFILE_FILE} exists but is not a file and was left unchanged"
        try:
            load_profile(project)
        except ProfileError as exc:
            return "invalid", f"{PROFILE_FILE} exists but is not usable and was left unchanged: {exc}"
        return "kept", str(path)
    tmp = path.with_name(f".profile.json.{os.getpid()}.tmp")
    try:
        payload = default_profile(project)
        parse_profile(payload)  # never write a profile this version would reject
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.link(tmp, path)  # fails if a profile appeared meanwhile, so a concurrent init cannot be overwritten
    except FileExistsError:
        return "kept", str(path)
    except (OSError, ProfileError) as exc:
        if path.exists():
            return "kept", str(path)
        return "invalid", f"could not create {PROFILE_FILE}: {exc}"  # init still succeeds; project-smoke will say the profile is missing
    finally:
        tmp.unlink(missing_ok=True)
    return "created", str(path)
