"""Default project profile created by `stagemesh init` when a project has none.

It uses the normal profile schema (see profile.py) and only conventions StageMesh already knows: the gates `detect_gates` finds at the
project root. When it finds none, the single gate is `git diff --check`, which is safe everywhere and proves nothing about behaviour;
the task type's summary says so. An existing profile, valid or not, is never touched.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .auto_plan import detect_gates
from .profile import PROFILE_FILE, ProfileError, load_profile, parse_profile, profile_path

# Secrets, local databases, caches, StageMesh runtime state and tool state; `**/x` does not match a root-level `x`, so both forms.
FORBIDDEN_FILES = (
    ".env", ".env.*", "**/.env", "**/.env.*", "*.pem", "**/*.pem", "*.key", "**/*.key", "id_rsa*", "**/id_rsa*",
    "*secret*", "**/*secret*", "credentials*.json", "**/credentials*.json",
    "*.sqlite", "*.sqlite3", "*.db", "**/*.sqlite", "**/*.sqlite3", "**/*.db",
    ".stagemesh/**", ".git/**", ".venv/**", "**/node_modules/**", "**/__pycache__/**", "**/.pytest_cache/**",
)
_FALLBACK_GATE = {"name": "default-acceptance-diff-check", "command": ["git", "diff", "--check"], "timeout_seconds": 120}


def _gate_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_") or "gate"


def default_profile(project: Path) -> dict[str, Any]:
    detected = detect_gates(project)
    gates = detected or [dict(_FALLBACK_GATE)]
    ids = {_gate_id(g["name"]): g for g in gates}
    if not any("acceptance" in g["name"].casefold() for g in gates):
        raise ProfileError("default profile needs an acceptance gate")
    summary = (
        "Generated default: runs the gates detected at the project root."
        if detected
        else "Generated default: no test gate was detected, so this only runs `git diff --check`; add real gates to .stagemesh/profile.json."
    )
    return {
        "schema_version": 1,
        "name": re.sub(r"[^A-Za-z0-9._-]+", "-", project.name) or "project",
        "forbidden_files": list(FORBIDDEN_FILES),
        "defaults": {"max_changed_files": 40, "max_diff_lines": 4000},
        "gates": {gid: {"name": g["name"], "command": g["command"], "timeout_seconds": g["timeout_seconds"]} for gid, g in ids.items()},
        "task_types": {"default": {"summary": summary, "allowed_files": ["**"], "validation_level": "full", "gates": list(ids)}},
        "type_selection": {"default_type": "default", "escalation_type": "default"},
    }


def ensure_profile(project: Path) -> tuple[str, str]:
    """('kept' | 'created' | 'invalid', detail). Never overwrites or repairs an existing profile file."""
    path = profile_path(project)
    if path.exists():
        try:
            load_profile(project)
        except ProfileError as exc:
            return "invalid", f"{PROFILE_FILE} exists but is not usable and was left unchanged: {exc}"
        return "kept", str(path)
    payload = default_profile(project)
    parse_profile(payload)  # never write a profile this version would reject
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".profile.json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        os.link(tmp, path)  # fails if a profile appeared meanwhile, so a concurrent init cannot be overwritten
    except FileExistsError:
        return "kept", str(path)
    except OSError:
        if path.exists():
            return "kept", str(path)
        os.replace(tmp, path)
        return "created", str(path)
    finally:
        tmp.unlink(missing_ok=True)
    return "created", str(path)
