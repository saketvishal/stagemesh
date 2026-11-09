"""Default project profile created by `stagemesh init` when a project has none.

It uses the normal profile schema (see profile.py) and only conventions StageMesh already knows: the gates `detect_gates` finds at the
project root. When it finds none, the single gate is `git diff --check`, which is safe everywhere and proves nothing about behaviour;
the task type's summary says so and automatic contract generation is disabled. An existing profile, valid or not, is never touched.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .auto_plan import FORBIDDEN_FILES as AUTO_PLAN_FORBIDDEN
from .auto_plan import detect_gates
from .profile import PROFILE_FILE, ProfileError, load_profile, parse_profile, profile_path

# The planner's own forbidden set (auto_plan), plus local databases, environments and caches.
FORBIDDEN_FILES = (*AUTO_PLAN_FORBIDDEN, "*.sqlite", "*.sqlite3", "*.db", ".venv/**", "**/node_modules/**", "**/__pycache__/**")
PLACEHOLDER_GATE = "default-acceptance-diff-check"  # project-smoke warns while this is the only gate
_FALLBACK_GATE = {"name": PLACEHOLDER_GATE, "command": ["git", "diff", "--check"], "timeout_seconds": 120}
_PYTHON_BOOTSTRAP_GATE = {
    "name": "project-acceptance-python-bootstrap",
    "command": ["stagemesh", "validation-bootstrap"],
    "timeout_seconds": 1800,
}


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


def suggested_test_command(project: Path) -> tuple[str, ...] | None:
    """A reviewable suggestion for projects that have test files but no conventional test configuration.

    The suggestion is never persisted or executed without explicit operator confirmation. A single direct Python test script is
    safer to suggest than inventing a framework dependency; multiple pytest-shaped files get the conventional pytest command.
    """
    tests = project / "tests"
    if not tests.is_dir() or tests.is_symlink():
        return None
    candidates = sorted(
        path
        for pattern in ("test_*.py", "*_test.py")
        for path in tests.glob(pattern)
        if path.is_file() and not path.is_symlink()
    )
    unique = list(dict.fromkeys(candidates))
    if len(unique) == 1:
        return ("python", unique[0].relative_to(project).as_posix())
    if unique:
        return ("python", "-m", "pytest", "-q")
    return None


def _python_project_needs_test_bootstrap(project: Path) -> bool:
    """Recognize Python source without walking private/dependency trees; the fixed gate, not init, runs candidate tests."""
    if any(path.is_file() and not path.is_symlink() for path in project.glob("*.py")):
        return True
    ignored = {".git", ".stagemesh", ".venv", "venv", "node_modules", "dist", "build"}
    try:
        children = tuple(project.iterdir())
    except OSError:
        return False
    return any(
        any(path.is_file() and not path.is_symlink() for path in child.glob("*.py"))
        for child in children
        if child.name not in ignored and child.is_dir() and not child.is_symlink()
    )


def default_profile(project: Path, test_command: Sequence[str] | None = None) -> dict[str, Any]:
    explicit = [str(part) for part in test_command or () if str(part)]
    detected = [{"name": "project-acceptance-user", "command": explicit, "timeout_seconds": 1800}] if explicit else [
        _argv(g) for g in detect_gates(project)
    ]
    bootstrap = not detected and _python_project_needs_test_bootstrap(project)
    if bootstrap:
        detected = [dict(_PYTHON_BOOTSTRAP_GATE)]
    gates = list(detected)
    if not any("acceptance" in g["name"].casefold() for g in gates):
        gates.append(dict(_FALLBACK_GATE))  # a `full` task type needs a broad gate; detected gates (e.g. a narrow smoke) may not be one
    ids = {_gate_id(g["name"]): g for g in gates}
    summary = (
        "Generated default: runs the explicitly confirmed project test command."
        if explicit
        else "Generated default: the first implementation must add at least one passing Python unittest under tests/."
        if bootstrap
        else "Generated default: runs the gates detected at the project root."
        if detected
        else "Generated default: no test gate was detected, so this only runs `git diff --check`; add real gates to .stagemesh/profile.json."
    )
    return {
        "schema_version": 1,
        "contract_generation": "automatic" if detected else "explicit_only",
        "name": re.sub(r"[^A-Za-z0-9._-]+", "-", project.name) or "project",
        "forbidden_files": list(FORBIDDEN_FILES),
        "defaults": {"max_changed_files": 60, "max_diff_lines": 6000},
        "gates": {gid: {"name": g["name"], "command": g["command"], "timeout_seconds": g["timeout_seconds"]} for gid, g in ids.items()},
        "task_types": {"default": {"summary": summary, "allowed_files": ["**"], "validation_level": "full", "gates": list(ids)}},
        "type_selection": {"default_type": "default", "escalation_type": "default"},
    }


def ensure_profile(project: Path, test_command: Sequence[str] | None = None) -> tuple[str, str]:
    """Create a profile or replace only StageMesh's exact placeholder after explicit test-command confirmation."""
    path = profile_path(project)
    if path.exists():
        if not path.is_file():
            return "invalid", f"{PROFILE_FILE} exists but is not a file and was left unchanged"
        try:
            existing = load_profile(project)
        except ProfileError as exc:
            return "invalid", f"{PROFILE_FILE} exists but is not usable and was left unchanged: {exc}"
        if test_command and existing is not None and is_placeholder(existing):
            tmp = path.with_name(f".profile.json.{os.getpid()}.tmp")
            try:
                original = path.read_bytes()
                payload = default_profile(project, test_command)
                parse_profile(payload)
                tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
                if path.read_bytes() != original:
                    return "kept", str(path)
                os.replace(tmp, path)
                return "configured", str(path)
            except (OSError, ProfileError) as exc:
                return "invalid", f"could not configure {PROFILE_FILE}: {exc}"
            finally:
                tmp.unlink(missing_ok=True)
        return "kept", str(path)
    tmp = path.with_name(f".profile.json.{os.getpid()}.tmp")
    try:
        payload = default_profile(project, test_command)
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
