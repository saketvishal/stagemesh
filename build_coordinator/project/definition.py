"""Project discovery: `.stagemesh/project.yaml` identifies a StageMesh project.

A project repository owns its configuration and task definitions (version
controlled under `.stagemesh/`). StageMesh owns volatile runtime state, which
lives in the project's *state directory* (default `.build-coordinator/`,
git-ignored) and never in task-definition files.

Nothing here reads a project-specific name: projects are found by walking up
from a directory, by explicit path, or by name/alias through a per-user
registry of project roots.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_DIR = ".stagemesh"
PROJECT_FILE = "project.yaml"
TASKS_DIR = "tasks"
SCHEMA_VERSION = 1
MAX_CONCURRENCY = 32

REGISTRY_ENV = "STAGEMESH_PROJECT_REGISTRY"
DEFAULT_REGISTRY_PATH = Path.home() / ".build-coordinator" / "projects.json"

_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_REVIEW_POLICIES = ("NONE", "SELF", "INDEPENDENT", "TWO_REVIEWERS")
_WORKER_ROLES = ("builder", "reviewer", "integration", "planner")


class ProjectError(ValueError):
    """Raised when a project cannot be discovered or its definition is invalid."""


@dataclass(frozen=True)
class ProjectDefinition:
    root: Path
    project_id: str
    name: str
    aliases: tuple[str, ...] = ()
    concurrency: int = 2
    reviewers: int = 1
    default_review_policy: str = "INDEPENDENT"
    main_ref: str = "main"
    remote_name: str = "origin"
    state_dir: Path = Path(".build-coordinator")
    worker_templates: dict[str, dict[str, Any]] = field(default_factory=dict)
    runner_config: Path | None = None
    task_sources: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def definition_dir(self) -> Path:
        return self.root / PROJECT_DIR

    @property
    def project_file(self) -> Path:
        return self.definition_dir / PROJECT_FILE

    @property
    def tasks_dir(self) -> Path:
        return self.definition_dir / TASKS_DIR

    @property
    def worktrees_dir(self) -> Path:
        return self.state_dir / "worktrees"

    def names(self) -> frozenset[str]:
        return frozenset(
            {_fold(self.project_id), _fold(self.name), *(_fold(alias) for alias in self.aliases)}
        )

    def summary(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "aliases": list(self.aliases),
            "root": str(self.root),
            "project_file": str(self.project_file),
            "tasks_dir": str(self.tasks_dir),
            "state_dir": str(self.state_dir),
            "concurrency": self.concurrency,
            "reviewers": self.reviewers,
            "default_review_policy": self.default_review_policy,
            "main_ref": self.main_ref,
            "worker_templates": sorted(self.worker_templates),
            "task_sources": sorted(self.task_sources),
        }


def _fold(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip().casefold())


def _canonical_root(root: Path) -> Path:
    """Map a linked git worktree back to the main working tree.

    Agents run inside StageMesh-managed worktrees that contain a copy of
    `.stagemesh/`; runtime state must still resolve to the one project root.
    """
    dot_git = root / ".git"
    if not dot_git.is_file():
        return root
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return root
    if proc.returncode != 0 or not proc.stdout.strip():
        return root
    common = Path(proc.stdout.strip())
    candidate = common.parent if common.name == ".git" else root
    if (candidate / PROJECT_DIR / PROJECT_FILE).is_file():
        return candidate.resolve()
    return root


def find_project_root(start: str | Path) -> Path | None:
    """Walk up from `start` looking for `.stagemesh/project.yaml`."""
    current = Path(start).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        if (directory / PROJECT_DIR / PROJECT_FILE).is_file():
            return _canonical_root(directory)
    return None


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - PyYAML is a core dependency
        raise ProjectError("PyYAML is required to read .stagemesh files") from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ProjectError(f"could not read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ProjectError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectError(f"{path} must contain a mapping")
    return data


def read_yaml(path: Path) -> dict[str, Any]:
    return _read_yaml(path)


def load_project(root: str | Path) -> ProjectDefinition:
    root_path = Path(root).expanduser().resolve()
    project_file = root_path / PROJECT_DIR / PROJECT_FILE
    if not project_file.is_file():
        raise ProjectError(f"not a StageMesh project: {project_file} does not exist")
    data = _read_yaml(project_file)

    problems: list[str] = []
    if data.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        problems.append(f"unsupported schema_version {data.get('schema_version')!r}")
    project_id = str(data.get("id") or "").strip()
    if not _PROJECT_ID.match(project_id):
        problems.append("`id` must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    name = str(data.get("name") or project_id).strip()
    aliases = tuple(str(a).strip() for a in (data.get("aliases") or ()) if str(a).strip())

    execution = data.get("execution") or {}
    if not isinstance(execution, dict):
        problems.append("`execution` must be a mapping")
        execution = {}
    concurrency = execution.get("concurrency", 2)
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or not 1 <= concurrency <= MAX_CONCURRENCY:
        problems.append(f"`execution.concurrency` must be an integer in 1..{MAX_CONCURRENCY}")
        concurrency = 2
    reviewers = execution.get("reviewers", 1)
    if not isinstance(reviewers, int) or isinstance(reviewers, bool) or not 1 <= reviewers <= MAX_CONCURRENCY:
        problems.append(f"`execution.reviewers` must be an integer in 1..{MAX_CONCURRENCY}")
        reviewers = 1
    review_policy = str(execution.get("default_review_policy") or "INDEPENDENT").upper()
    if review_policy not in _REVIEW_POLICIES:
        problems.append(f"`execution.default_review_policy` must be one of {_REVIEW_POLICIES}")

    repository = data.get("repository") or {}
    if not isinstance(repository, dict):
        problems.append("`repository` must be a mapping")
        repository = {}

    raw_state = str(data.get("state_dir") or ".build-coordinator")
    state_dir = Path(raw_state).expanduser()
    if not state_dir.is_absolute():
        state_dir = root_path / state_dir
    state_dir = state_dir.resolve()

    templates: dict[str, dict[str, Any]] = {}
    workers = data.get("workers") or {}
    if not isinstance(workers, dict):
        problems.append("`workers` must be a mapping of role -> worker template")
        workers = {}
    for role, template in workers.items():
        if role not in _WORKER_ROLES:
            problems.append(f"unknown worker role {role!r}; expected one of {_WORKER_ROLES}")
        elif not isinstance(template, dict):
            problems.append(f"`workers.{role}` must be a mapping")
        else:
            templates[role] = dict(template)

    runner_config = data.get("runner_config")
    runner_path: Path | None = None
    if runner_config:
        runner_path = Path(str(runner_config)).expanduser()
        if not runner_path.is_absolute():
            runner_path = (root_path / PROJECT_DIR / runner_path).resolve()
        if not runner_path.is_file():
            problems.append(f"`runner_config` file does not exist: {runner_path}")

    sources = data.get("task_sources") or {}
    if not isinstance(sources, dict):
        problems.append("`task_sources` must be a mapping")
        sources = {}

    if problems:
        raise ProjectError(f"invalid project definition {project_file}: " + "; ".join(problems))

    return ProjectDefinition(
        root=root_path,
        project_id=project_id,
        name=name,
        aliases=aliases,
        concurrency=concurrency,
        reviewers=reviewers,
        default_review_policy=review_policy,
        main_ref=str(repository.get("main_ref") or "main"),
        remote_name=str(repository.get("remote_name") or "origin"),
        state_dir=state_dir,
        worker_templates=templates,
        runner_config=runner_path,
        task_sources={str(k): dict(v or {}) for k, v in sources.items()},
    )


def registry_path() -> Path:
    configured = os.getenv(REGISTRY_ENV)
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_REGISTRY_PATH


def registered_roots() -> list[Path]:
    path = registry_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectError(f"project registry {path} is unreadable: {exc}") from exc
    return [Path(str(item)) for item in data.get("projects", [])]


def register_project(root: str | Path) -> ProjectDefinition:
    project = load_project(root)
    roots = [str(p) for p in registered_roots()]
    entry = str(project.root)
    others = []
    for existing in roots:
        try:
            other = load_project(existing)
        except ProjectError:
            others.append(existing)
            continue
        if other.project_id == project.project_id and other.root != project.root:
            continue
        if other.root != project.root:
            others.append(existing)
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"projects": sorted({*others, entry})}, indent=2) + "\n", encoding="utf-8"
    )
    return project


def registered_projects() -> list[ProjectDefinition]:
    projects: list[ProjectDefinition] = []
    for root in registered_roots():
        try:
            projects.append(load_project(root))
        except ProjectError:
            continue
    return projects


def resolve_project(
    query: str | None = None,
    *,
    path: str | Path | None = None,
    cwd: str | Path | None = None,
) -> ProjectDefinition:
    """Resolve the project to operate on.

    Order: explicit `path`; then `query` (name/alias/id) against the registry
    and the project containing `cwd`; then the project containing `cwd`.
    """
    if path is not None:
        root = find_project_root(path)
        if root is None:
            raise ProjectError(f"no {PROJECT_DIR}/{PROJECT_FILE} found at or above {path}")
        return load_project(root)

    here = find_project_root(cwd if cwd is not None else Path.cwd())
    if query:
        wanted = _fold(query)
        matches = [p for p in registered_projects() if wanted in p.names()]
        if here is not None:
            local = load_project(here)
            if wanted in local.names() and all(local.root != m.root for m in matches):
                matches.append(local)
        if not matches:
            known = ", ".join(sorted({p.project_id for p in registered_projects()})) or "none registered"
            raise ProjectError(
                f"no StageMesh project named {query!r} (registered: {known}). "
                "Register one with `stagemesh project register <path>`."
            )
        if len({m.root for m in matches}) > 1:
            roots = ", ".join(str(m.root) for m in matches)
            raise ProjectError(f"project name {query!r} is ambiguous: {roots}")
        return matches[0]
    if here is None:
        raise ProjectError(
            "no StageMesh project specified and none found above the current directory; "
            "name one (`stagemesh continue <project>`) or register it first"
        )
    return load_project(here)


_CONTINUE_PHRASE = re.compile(r"^\s*continue\b\s*(?P<rest>.*?)\s*[.!]*\s*$", re.IGNORECASE)


def parse_continue_phrase(words: list[str]) -> str | None:
    """Extract the project name from "Continue <name> development." phrasing.

    Returns "" when the phrase names no project (use the cwd project) and
    None when the words are not a continue phrase.
    """
    phrase = " ".join(words)
    match = _CONTINUE_PHRASE.match(phrase)
    if not match:
        return None
    rest = re.sub(r"\bdevelopment\b\s*$", "", match.group("rest"), flags=re.IGNORECASE).strip()
    return rest
