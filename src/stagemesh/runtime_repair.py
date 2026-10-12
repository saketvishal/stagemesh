from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class RuntimeContamination:
    project_runtime: Path
    venv: Path
    imported_package_path: Path
    imported_package_outside_project_runtime: bool
    pythonpath_entries: tuple[Path, ...]
    pythonpath_contaminated: bool
    next: str | None

    @property
    def contaminated(self) -> bool:
        return self.imported_package_outside_project_runtime or self.pythonpath_contaminated

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_runtime": str(self.project_runtime),
            "venv": str(self.venv),
            "imported_package_path": str(self.imported_package_path),
            "imported_package_outside_project_runtime": self.imported_package_outside_project_runtime,
            "pythonpath_entries": [str(path) for path in self.pythonpath_entries],
            "pythonpath_contaminated": self.pythonpath_contaminated,
            "contaminated": self.contaminated,
            "next": self.next,
        }


@dataclass(frozen=True)
class RuntimeRepairResult:
    project: Path
    venv: Path
    python: Path
    source: Path
    dry_run: bool
    contaminated: bool
    removals: tuple[Path, ...]
    backups: tuple[dict[str, str], ...]
    reinstall_command: tuple[str, ...]
    reinstalled: bool
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": str(self.project),
            "venv": str(self.venv),
            "python": str(self.python),
            "source": str(self.source),
            "dry_run": self.dry_run,
            "contaminated": self.contaminated,
            "removals": [str(path) for path in self.removals],
            "backups": list(self.backups),
            "reinstall_command": list(self.reinstall_command),
            "reinstalled": self.reinstalled,
            "limitations": list(self.limitations),
        }


def project_runtime_venv(project: Path) -> Path:
    return Path(project).resolve() / ".stagemesh" / "tooling" / "venv"


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def detect_runtime_contamination(
    project: Path,
    *,
    imported_package_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeContamination:
    import stagemesh

    project = Path(project).resolve()
    venv = project_runtime_venv(project)
    runtime = project / ".stagemesh"
    imported = Path(imported_package_path or Path(stagemesh.__file__).resolve().parent).resolve()
    env = os.environ if environ is None else environ
    pythonpath_entries = tuple(Path(item).resolve() for item in env.get("PYTHONPATH", "").split(os.pathsep) if item)
    outside = not _inside(imported, venv)
    pythonpath_contaminated = any(not _inside(entry, venv) and _looks_like_stagemesh_source(entry) for entry in pythonpath_entries)
    contaminated = outside or pythonpath_contaminated
    return RuntimeContamination(
        project_runtime=runtime,
        venv=venv,
        imported_package_path=imported,
        imported_package_outside_project_runtime=outside,
        pythonpath_entries=pythonpath_entries,
        pythonpath_contaminated=pythonpath_contaminated,
        next="stagemesh runtime repair --write" if contaminated else None,
    )


def plan_runtime_repair(
    project: Path,
    *,
    source: Path | None = None,
    imported_package_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeRepairResult:
    project = Path(project).resolve()
    venv = project_runtime_venv(project)
    py = venv_python(venv)
    install_source = _install_source(source, imported_package_path)
    contamination = detect_runtime_contamination(project, imported_package_path=imported_package_path, environ=environ)
    removals = tuple(_editable_metadata(project, venv, imported_package_path=imported_package_path))
    limitations: list[str] = []
    if not py.exists():
        limitations.append(f"project-local python not found: {py}")
    contaminated = contamination.contaminated or bool(removals)
    return RuntimeRepairResult(
        project=project,
        venv=venv,
        python=py,
        source=install_source,
        dry_run=True,
        contaminated=contaminated,
        removals=removals,
        backups=(),
        reinstall_command=(str(py), "-m", "pip", "install", "--upgrade", str(install_source)) if contaminated else (),
        reinstalled=False,
        limitations=tuple(limitations),
    )


def repair_runtime(
    project: Path,
    *,
    source: Path | None = None,
    imported_package_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> RuntimeRepairResult:
    planned = plan_runtime_repair(project, source=source, imported_package_path=imported_package_path, environ=environ)
    if planned.limitations:
        return RuntimeRepairResult(**{**planned.__dict__, "dry_run": False})
    if not planned.contaminated:
        return RuntimeRepairResult(**{**planned.__dict__, "dry_run": False})
    backups: list[dict[str, str]] = []
    backup_root = planned.project / ".stagemesh" / "tooling" / "repair-backups" / time.strftime("%Y%m%d-%H%M%S")
    for path in planned.removals:
        if not path.exists():
            continue
        target = backup_root / _relative_to_venv(path, planned.venv)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        backups.append({"removed": str(path), "backup": str(target)})
    proc = runner(
        list(planned.reinstall_command),
        cwd=planned.project,
        text=True,
        capture_output=True,
        check=False,
    )
    limitations = list(planned.limitations)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-1000:]
        limitations.append(f"pip reinstall failed with exit {proc.returncode}: {detail}")
    return RuntimeRepairResult(
        project=planned.project,
        venv=planned.venv,
        python=planned.python,
        source=planned.source,
        dry_run=False,
        contaminated=planned.contaminated,
        removals=planned.removals,
        backups=tuple(backups),
        reinstall_command=planned.reinstall_command,
        reinstalled=proc.returncode == 0,
        limitations=tuple(limitations),
    )


def _install_source(source: Path | None, imported_package_path: Path | None) -> Path:
    if source is not None:
        return Path(source).resolve()
    if imported_package_path is not None:
        root = _source_root(Path(imported_package_path).resolve())
        if root is not None:
            return root
    here = Path(__file__).resolve()
    root = _source_root(here.parent)
    return root or here.parents[2]


def _source_root(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "stagemesh").is_dir():
            return candidate
    return None


def _editable_metadata(project: Path, venv: Path, *, imported_package_path: Path | None = None) -> list[Path]:
    site_packages = [path for path in _site_packages(venv) if path.exists()]
    paths: list[Path] = []
    imported = Path(imported_package_path).resolve() if imported_package_path is not None else None
    for site in site_packages:
        for path in site.glob("*.pth"):
            text = _read_text(path)
            if _is_contaminating_text(text, project, venv, imported):
                paths.append(path)
        for path in site.glob("*.egg-link"):
            text = _read_text(path)
            if "stagemesh" in path.name.casefold() or _is_contaminating_text(text, project, venv, imported):
                paths.append(path)
        for path in site.glob("__editable__*stagemesh*.py"):
            paths.append(path)
        for path in site.glob("stagemesh*.dist-info/direct_url.json"):
            if _editable_direct_url(path, project, venv):
                paths.append(path)
    return sorted({path.resolve(): path for path in paths}.values(), key=lambda item: str(item).casefold())


def _site_packages(venv: Path) -> list[Path]:
    candidates = [venv / "Lib" / "site-packages"]
    candidates.extend((venv / "lib").glob("python*/site-packages"))
    return candidates


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _is_contaminating_text(text: str, project: Path, venv: Path, imported: Path | None) -> bool:
    lowered = text.casefold()
    if "stagemesh" not in lowered and "__editable__" not in lowered:
        return False
    for raw in text.replace("\r", "\n").split("\n"):
        value = raw.strip().strip('"').strip("'")
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if _inside(resolved, venv):
            continue
        if imported is not None and (_inside(imported, resolved) or _inside(resolved, imported)):
            return True
        if _looks_like_stagemesh_source(resolved) or not _inside(resolved, project):
            return True
    return "__editable__" in lowered


def _editable_direct_url(path: Path, project: Path, venv: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("dir_info"), dict):
        return False
    if data["dir_info"].get("editable") is not True:
        return False
    url = str(data.get("url") or "")
    if not url.startswith("file://"):
        return True
    source = Path(url.removeprefix("file://")).resolve()
    return not _inside(source, venv) and (_looks_like_stagemesh_source(source) or not _inside(source, project))


def _looks_like_stagemesh_source(path: Path) -> bool:
    return (path / "stagemesh").is_dir() or (path / "src" / "stagemesh").is_dir()


def _relative_to_venv(path: Path, venv: Path) -> Path:
    try:
        return path.resolve().relative_to(venv.resolve())
    except ValueError:
        return Path(path.name)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path = path.resolve()
        parent = parent.resolve()
    except OSError:
        pass
    return path == parent or parent in path.parents
