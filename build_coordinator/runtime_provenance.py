"""Runtime provenance diagnostics for the active StageMesh controller."""

from __future__ import annotations

import importlib.metadata as metadata
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeProvenance:
    controller_executable: str | None
    controller_python: str
    package_path: str
    package_version: str | None
    controller_revision: str | None
    controller_dirty: bool | None
    project_root: str | None
    project_validation_interpreter: str
    coordinator_database: str | None
    conflicts: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "controller_executable": self.controller_executable,
            "controller_python": self.controller_python,
            "package_path": self.package_path,
            "package_version": self.package_version,
            "controller_revision": self.controller_revision,
            "controller_dirty": self.controller_dirty,
            "project_root": self.project_root,
            "project_validation_interpreter": self.project_validation_interpreter,
            "coordinator_database": self.coordinator_database,
            "conflicts": list(self.conflicts),
        }


def collect_runtime_provenance(
    *,
    project_root: Path | None = None,
    coordinator_database: str | None = None,
    package_root: Path | None = None,
) -> RuntimeProvenance:
    import build_coordinator

    package_path = Path(package_root or build_coordinator.__file__).resolve()
    source_root = _source_root_from_package_path(package_path)
    expected_root = _expected_control_root()
    conflicts: list[dict[str, Any]] = []
    if expected_root is not None and source_root != expected_root:
        conflicts.append(
            _diagnostic(
                "STAGEMESH_CONTROL_PLANE_MISMATCH",
                "active build_coordinator package does not come from the configured StageMesh control-plane root",
                expected=str(expected_root),
                actual=str(source_root),
            )
        )
    for candidate in _candidate_package_roots(source_root):
        conflicts.append(
            _diagnostic(
                "STAGEMESH_CONFLICTING_INSTALLATION",
                "another build_coordinator package is importable from this interpreter context",
                expected=str(source_root),
                actual=str(candidate),
            )
        )
    return RuntimeProvenance(
        controller_executable=shutil.which("stagemesh"),
        controller_python=sys.executable,
        package_path=str(source_root),
        package_version=_package_version(),
        controller_revision=_git(source_root, "rev-parse", "HEAD"),
        controller_dirty=_git_dirty(source_root),
        project_root=str(project_root.resolve()) if project_root else None,
        project_validation_interpreter=sys.executable,
        coordinator_database=coordinator_database,
        conflicts=tuple(conflicts),
    )


def _expected_control_root() -> Path | None:
    configured = os.getenv("STAGEMESH_CONTROL_PLANE_ROOT")
    if not configured:
        return None
    return Path(configured).expanduser().resolve()


def _source_root_from_package_path(package_path: Path) -> Path:
    if package_path.name == "__init__.py":
        return package_path.parent.parent
    if package_path.name == "build_coordinator":
        return package_path.parent
    return package_path


def _candidate_package_roots(active_root: Path) -> tuple[Path, ...]:
    roots: set[Path] = set()
    for raw in sys.path:
        if not raw:
            continue
        base = Path(raw).expanduser()
        candidate = base / "build_coordinator" / "__init__.py"
        try:
            if candidate.exists():
                root = base.resolve()
                if root != active_root:
                    roots.add(root)
        except OSError:
            continue
    return tuple(sorted(roots, key=str))


def _package_version() -> str | None:
    import build_coordinator

    try:
        return metadata.version("stagemesh")
    except metadata.PackageNotFoundError:
        pass
    version = getattr(build_coordinator, "__version__", None)
    if version:
        return str(version)
    try:
        return metadata.version("build-coordinator")
    except metadata.PackageNotFoundError:
        return None


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _git_dirty(root: Path) -> bool | None:
    out = _git(root, "status", "--porcelain")
    return None if out is None else bool(out)


def _diagnostic(code: str, message: str, *, expected: str, actual: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "expected": expected,
        "actual": actual,
        "action": "launch StageMesh with the intended interpreter/source root or remove the stale editable install from this execution environment",
    }
