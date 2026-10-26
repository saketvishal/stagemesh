"""What StageMesh is running as: version, where it is installed, whether it is a source checkout, and where it was built from.

A production install is a normal (non-editable) package in a standalone tool environment that no project owns. An editable install or
a bare `PYTHONPATH=src` run is a development runtime: it follows a source checkout, so it is reported as such.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict
from importlib import metadata
from pathlib import Path
from typing import Any

from . import __version__

RUNTIME_HOME_ENV = "STAGEMESH_RUNTIME_HOME"


def runtime_home() -> Path:
    """Root of the standalone StageMesh tool environment (`<home>/venv`); not inside any project or source checkout."""
    override = os.environ.get(RUNTIME_HOME_ENV)
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "StageMesh" / "runtime"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "stagemesh" / "runtime"


def runtime_venv() -> Path:
    return runtime_home() / "venv"


def runtime_cli(venv: Path | None = None) -> Path:
    venv = venv or runtime_venv()
    return venv / "Scripts" / "stagemesh.exe" if os.name == "nt" else venv / "bin" / "stagemesh"


def _dist() -> metadata.Distribution | None:
    try:
        return metadata.distribution("stagemesh")
    except metadata.PackageNotFoundError:
        return None


def _direct_url(dist: metadata.Distribution | None) -> dict[str, Any]:
    try:
        text = dist.read_text("direct_url.json") if dist else None
        data = json.loads(text) if text else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _editable_pth(dist: metadata.Distribution | None) -> str | None:
    for item in (dist.files or []) if dist else []:
        if item.name.endswith(".pth") and "editable" in item.name.lower():
            return str(dist.locate_file(item))
    return None


def _build_info() -> dict[str, Any]:
    try:
        from . import (
            _build_info as info,  # generated into the wheel by build_backend; absent in a source checkout
        )

        return {"source_commit": info.SOURCE_COMMIT, "source_dirty": info.SOURCE_DIRTY, "built_from": info.BUILT_FROM}
    except ImportError:
        return {}


def runtime_info() -> dict[str, Any]:
    package_dir = Path(__file__).resolve().parent
    dist = _dist()
    direct = _direct_url(dist)
    pth = _editable_pth(dist)
    editable = bool(pth) or bool((direct.get("dir_info") or {}).get("editable"))
    in_site_packages = any(part in ("site-packages", "dist-packages") for part in package_dir.parts)
    # Anything not living in site-packages is a source checkout (editable install, PYTHONPATH=src, or running from the tree).
    from_source = editable or not in_site_packages
    return {
        "version": __version__,
        "dist_version": dist.version if dist else None,
        "package_path": str(package_dir),
        "interpreter": sys.executable,
        "environment": sys.prefix,
        "standalone_runtime": str(runtime_venv()),
        "in_standalone_runtime": _is_inside(Path(sys.prefix), runtime_venv()),
        "editable": editable,
        "editable_target": pth and Path(pth).read_text(encoding="utf-8", errors="replace").strip() or None,
        "source_checkout": from_source,
        "mode": "development (editable/source checkout)" if from_source else "installed",
        "provenance": {**_build_info(), "install_source": (direct.get("url") if direct else None)},
    }


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _source_evidence(package_dir: Path) -> dict[str, Any]:
    """Observe the loaded source tree, never the caller's project or an install URL. Git errors remain unknown."""
    unknown = {"source_commit": None, "source_dirty": None, "status": "UNKNOWN"}
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
            cwd=package_dir,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=2,
            check=True,
        )
        return result.stdout.strip()

    try:
        root_text, commit = git("rev-parse", "--show-toplevel", "HEAD").splitlines()
        root = Path(root_text).resolve()
        if (root / "src" / "stagemesh").resolve() != package_dir or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            return {**unknown, "reason": "LOADED_PACKAGE_NOT_VERIFIED"}
        # An untracked package beneath an unrelated repository must not inherit that repository's commit identity.
        git("ls-files", "--error-unmatch", "--", "runtime_info.py")
        dirty = bool(git("status", "--porcelain=v1", "--untracked-files=normal"))
        return {"source_commit": commit, "source_dirty": dirty, "status": "VERIFIED"}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {**unknown, "reason": "GIT_PROVENANCE_UNAVAILABLE"}


def runtime_evidence() -> dict[str, Any]:
    """Small, credential-free runtime snapshot for durable window evidence; no provider or network calls.

    Checkout evidence observes the current source tree, rather than asserting that it is immutable for the whole window.
    Installed wheels use their embedded build provenance. Unknown provenance is explicit and never replaced by another checkout.
    """
    from .process_identity import owner_identity

    package_dir = Path(__file__).resolve().parent
    from_source = not any(part in ("site-packages", "dist-packages") for part in package_dir.parts)
    if from_source:
        provenance = {"kind": "source_checkout", **_source_evidence(package_dir)}
    else:
        built = _build_info()
        commit, dirty = built.get("source_commit"), built.get("source_dirty")
        verified = isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is not None and isinstance(dirty, bool)
        provenance = {
            "kind": "wheel_build",
            "source_commit": commit if verified else None,
            "source_dirty": dirty if verified else None,
            "status": "VERIFIED" if verified else "UNKNOWN",
        }
    dist = _dist()
    return {
        "version": __version__,
        "dist_version": dist.version if dist else None,
        "package_path": str(package_dir),
        "interpreter": sys.executable,
        "process_identity": asdict(owner_identity()),
        "provenance": provenance,
    }


def format_runtime(info: dict[str, Any]) -> list[str]:
    prov = info["provenance"]
    lines = [
        f"stagemesh runtime: {info['mode']}",
        f"  version: {info['version']}",
        f"  package path: {info['package_path']}",
        f"  environment: {info['environment']}" + (" (standalone runtime)" if info["in_standalone_runtime"] else ""),
        f"  editable install: {'YES -> ' + str(info['editable_target'] or 'yes') if info['editable'] else 'no'}",
        f"  source checkout: {'YES' if info['source_checkout'] else 'no'}",
    ]
    if prov.get("source_commit"):
        lines.append(f"  built from: {prov['source_commit']}{' (dirty tree)' if prov.get('source_dirty') else ''}")
    if prov.get("built_from"):
        lines.append(f"  built from checkout: {prov['built_from']}")
    return lines
