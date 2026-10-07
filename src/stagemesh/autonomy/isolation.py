"""Fail-closed runtime isolation: this checkout's StageMesh state must never resolve into another StageMesh checkout.

Checked paths: the `.stagemesh` runtime, SQLite state, task worktrees, logs, temp directories and agent execution state, plus the
git object store the checkout uses, every registered git worktree, any configured database URL and any global registry. Paths are
compared after resolving symlinks and junctions. "Another StageMesh checkout" is any directory listed as forbidden (argument,
`STAGEMESH_FORBIDDEN_CHECKOUTS`, or `.stagemesh/isolation.json`) and, heuristically, any enclosing git checkout that looks like
StageMesh (its own `.stagemesh` runtime, a `src/stagemesh` package or a stagemesh `pyproject.toml`).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ConfigValidationError
from ..git import GitError, GitWorkspace
from ..workspaces import worktree_root

FORBIDDEN_ENV = "STAGEMESH_FORBIDDEN_CHECKOUTS"
ISOLATION_FILE = "isolation.json"
RUNTIME_SUBDIRS = {"logs": "logs", "tmp": "tmp", "agents": "agents", "autonomy": "autonomy"}


class IsolationViolation(RuntimeError):
    def __init__(self, report: IsolationReport):
        self.report = report
        super().__init__("; ".join(f"[{f.code}] {f.message}" for f in report.findings))


@dataclass(frozen=True)
class IsolationFinding:
    code: str
    message: str = ""
    path: str = ""
    other_checkout: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path, "other_checkout": self.other_checkout}


@dataclass
class IsolationReport:
    project: str
    paths: dict[str, str] = field(default_factory=dict)
    forbidden_checkouts: list[str] = field(default_factory=list)
    findings: list[IsolationFinding] = field(default_factory=list)

    @property
    def isolated(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "isolated": self.isolated,
            "paths": self.paths,
            "forbidden_checkouts": self.forbidden_checkouts,
            "findings": [f.to_dict() for f in self.findings],
        }


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(str(path))).rstrip("\\/")


def _inside(path: Path | str, parent: Path | str) -> bool:
    child, base = _norm(path), _norm(parent)
    return child == base or child.startswith(base + os.sep)


def _looks_like_stagemesh_checkout(directory: Path) -> bool:
    if not (directory / ".git").exists():
        return False
    if (directory / ".stagemesh").is_dir() or (directory / "src" / "stagemesh").is_dir():
        return True
    pyproject = directory / "pyproject.toml"
    try:
        return bool(re.search(r'^name\s*=\s*"stagemesh"', pyproject.read_text(encoding="utf-8"), re.MULTILINE))
    except OSError:
        return False


def _enclosing_stagemesh_checkout(path: Path | str, own_project: Path) -> Path | None:
    """The nearest enclosing StageMesh checkout of `path` that is not `own_project`, if any."""
    current = Path(os.path.realpath(str(path)))
    if _inside(current, own_project):
        return None  # our own task worktrees are StageMesh-shaped checkouts too; anything under the project is ours
    for candidate in (current, *current.parents):
        if _looks_like_stagemesh_checkout(candidate):
            return candidate
    return None


class UnreadableIsolationConfig(ValueError):
    pass


def _declared_forbidden(config: Path) -> list[Path]:
    """The forbidden list from isolation.json. An unreadable or malformed file raises: forgetting the list would fail open."""
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
        items = data["forbidden_checkouts"] if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ValueError("isolation.json must be an object with a forbidden_checkouts list")  # noqa: TRY004
        return [Path(str(item)) for item in items if str(item).strip()]
    except (OSError, ValueError, KeyError) as exc:
        raise UnreadableIsolationConfig(f"{config} is unreadable or malformed: {exc}") from exc


def configured_forbidden_checkouts(project: Path, explicit: Iterable[Path | str] = ()) -> list[Path]:
    found: list[Path] = [Path(item) for item in explicit]
    found += [Path(item) for item in os.environ.get(FORBIDDEN_ENV, "").split(os.pathsep) if item.strip()]
    config = Path(project) / ".stagemesh" / ISOLATION_FILE
    if config.exists():
        found += _declared_forbidden(config)
    unique: dict[str, Path] = {}
    for item in found:
        unique.setdefault(_norm(item), item)
    return list(unique.values())


def runtime_paths(project: Path) -> tuple[dict[str, Path], list[IsolationFinding]]:
    """Every location this checkout writes StageMesh state to."""
    project = Path(project).resolve()
    runtime = project / ".stagemesh"
    paths: dict[str, Path] = {
        "runtime": runtime,
        "sqlite": runtime / "stagemesh.sqlite3",
        **{name: runtime / sub for name, sub in RUNTIME_SUBDIRS.items()},
    }
    findings: list[IsolationFinding] = []
    try:
        paths["worktrees"] = worktree_root(project)
    except (ConfigValidationError, OSError, ValueError) as exc:
        findings.append(IsolationFinding("UNSAFE_RUNTIME_CONFIG", f"runtime configuration is invalid: {exc}"))
        paths["worktrees"] = runtime / "worktrees"
    return paths, findings


def check_isolation(
    project: Path,
    *,
    forbidden_checkouts: Iterable[Path | str] = (),
    registry_path: Path | str | None = None,
    database_url: str | None = None,
    extra_paths: Mapping[str, Path | str] | None = None,
    allow_external_runtime_paths: bool = False,
    check_running_code: bool = False,
    expected_code_checkout: Path | str | None = None,
) -> IsolationReport:
    project = Path(project).resolve()
    paths, findings = runtime_paths(project)
    for name, value in (extra_paths or {}).items():
        paths[name] = Path(value)
    try:
        forbidden = configured_forbidden_checkouts(project, forbidden_checkouts)
    except UnreadableIsolationConfig as exc:
        forbidden = []
        findings.append(IsolationFinding("UNREADABLE_ISOLATION_CONFIG", str(exc), str(project / ".stagemesh" / ISOLATION_FILE)))
    report = IsolationReport(str(project), {k: str(v) for k, v in paths.items()}, [str(p) for p in forbidden], findings)
    runtime = project / ".stagemesh"

    for forbidden_path in forbidden:
        if _inside(project, forbidden_path) or _inside(forbidden_path, project):
            report.findings.append(
                IsolationFinding("PROJECT_OVERLAPS_FORBIDDEN_CHECKOUT", f"project {project} overlaps forbidden checkout {forbidden_path}", str(project), str(forbidden_path))
            )

    for name, path in paths.items():
        resolved = Path(os.path.realpath(str(path)))
        for forbidden_path in forbidden:
            if _inside(resolved, forbidden_path):
                report.findings.append(
                    IsolationFinding(
                        "PATH_RESOLVES_INTO_OTHER_CHECKOUT",
                        f"{name} path {path} resolves to {resolved}, inside another StageMesh checkout {forbidden_path}",
                        str(resolved),
                        str(forbidden_path),
                    )
                )
        other = _enclosing_stagemesh_checkout(resolved, project)
        if other is not None and not any(f.path == str(resolved) and f.code == "PATH_RESOLVES_INTO_OTHER_CHECKOUT" for f in report.findings):
            report.findings.append(
                IsolationFinding(
                    "PATH_RESOLVES_INTO_OTHER_CHECKOUT",
                    f"{name} path {path} resolves to {resolved}, inside another StageMesh checkout {other}",
                    str(resolved),
                    str(other),
                )
            )
        if not allow_external_runtime_paths and not _inside(resolved, runtime):
            report.findings.append(
                IsolationFinding("PATH_OUTSIDE_RUNTIME", f"{name} path {path} resolves to {resolved}, outside this checkout's .stagemesh runtime", str(resolved))
            )

    _check_git_store(project, report)
    _check_shared_state(project, report, registry_path, database_url)
    if check_running_code or expected_code_checkout is not None:
        _check_running_code(project, report, Path(expected_code_checkout) if expected_code_checkout is not None else None)
    return report


def _project_local_install_root(project: Path) -> Path:
    return project / ".stagemesh" / "tooling" / "venv"


def _check_running_code(project: Path, report: IsolationReport, expected_checkout: Path | None = None) -> None:
    """The StageMesh code executing must come from the expected checkout or this project's owned tool runtime.

    By default, a StageMesh-shaped project may run either from its source tree or from its project-local bootstrap install under
    `.stagemesh/tooling/venv`. Ordinary projects may use any installed StageMesh. When `expected_checkout` is a different checkout,
    only that checkout is accepted as the tool source.
    """
    own_package = (expected_checkout or project) / "src" / "stagemesh"
    if expected_checkout is None and not own_package.is_dir():
        return
    import stagemesh

    running = Path(os.path.realpath(stagemesh.__file__)).parent
    if _inside(running, own_package):
        return
    allow_project_local = expected_checkout is None or _norm(expected_checkout) == _norm(project)
    if allow_project_local and _inside(running, _project_local_install_root(project)):
        return
    expected = f"{own_package}"
    if allow_project_local:
        expected += f" or {_project_local_install_root(project)}"
    report.findings.append(
        IsolationFinding(
            "RUNNING_CODE_FROM_OTHER_CHECKOUT",
            f"StageMesh code is being imported from {running}, not from {expected}; run this checkout's project-local StageMesh install",
            str(running),
            str(running.parent.parent),
        )
    )


def _is_stagemesh_candidate_worktree(path: Path | str) -> bool:
    """Short-lived provider validation worktrees are owned by this run, even though Git records them outside the checkout."""
    resolved = Path(os.path.realpath(str(path)))
    if resolved.name != "checkout":
        return False
    parent = resolved.parent
    if not parent.name.startswith("stagemesh-candidate-"):
        return False
    return _inside(parent, tempfile.gettempdir())


def _check_git_store(project: Path, report: IsolationReport) -> None:
    git = GitWorkspace(project)
    try:
        toplevel = git.run("rev-parse", "--show-toplevel").stdout.strip()
        common = git.run("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        listing = git.run("worktree", "list", "--porcelain").stdout
    except (GitError, OSError) as exc:
        report.findings.append(IsolationFinding("NOT_A_GIT_CHECKOUT", f"cannot inspect git state of {project}: {exc}"))
        return
    if _norm(toplevel) != _norm(project):
        report.findings.append(
            IsolationFinding("PROJECT_NOT_REPOSITORY_ROOT", f"{project} is inside the repository rooted at {toplevel}; it must be its own checkout root", toplevel)
        )
    if not _inside(common, project):
        report.findings.append(
            IsolationFinding("SHARED_GIT_STORE", f"git object store {common} is outside this checkout; it is shared with another checkout", common)
        )
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        entry = line[len("worktree ") :].strip()
        if not _inside(entry, project):
            if _is_stagemesh_candidate_worktree(entry):
                continue
            report.findings.append(
                IsolationFinding("FOREIGN_WORKTREE", f"git worktree {entry} is registered in this checkout's git store but lives outside it", entry)
            )


def _check_shared_state(project: Path, report: IsolationReport, registry_path: Path | str | None, database_url: str | None) -> None:
    url = database_url if database_url is not None else os.environ.get("STAGEMESH_DATABASE_URL")
    if url and not _sqlite_url_inside(url, project / ".stagemesh"):
        report.findings.append(
            IsolationFinding("SHARED_DATABASE", "STAGEMESH_DATABASE_URL points outside this checkout's SQLite state; refusing to share durable state")
        )
    if registry_path is not None and not _inside(registry_path, project):
        report.findings.append(
            IsolationFinding("SHARED_REGISTRY", f"global registry {registry_path} is shared across checkouts", str(registry_path))
        )


def _sqlite_url_inside(url: str, runtime: Path) -> bool:
    match = re.match(r"^sqlite:(?://)?/?(?P<path>.+)$", url.strip(), re.IGNORECASE)
    return bool(match and _inside(match.group("path"), runtime))


def require_isolation(project: Path, **kwargs: Any) -> IsolationReport:
    """Return the report when isolated; raise `IsolationViolation` (fail closed) otherwise."""
    report = check_isolation(project, **kwargs)
    if not report.isolated:
        raise IsolationViolation(report)
    return report
