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

from build_coordinator.policy import normalize_review_policy

PROJECT_DIR = ".stagemesh"
PROJECT_FILE = "project.yaml"
TASKS_DIR = "tasks"
SCHEMA_VERSION = 1
MAX_CONCURRENCY = 32

REGISTRY_ENV = "STAGEMESH_PROJECT_REGISTRY"
DEFAULT_REGISTRY_PATH = Path.home() / ".build-coordinator" / "projects.json"

_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_REVIEW_POLICIES = (
    "NONE",
    "SELF",
    "INDEPENDENT",
    "INDEPENDENT_WORKER",
    "INDEPENDENT_PROVIDER",
    "TWO_REVIEWERS",
    "TWO_PROVIDERS",
)
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
    worker_templates: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    runner_config: Path | None = None
    task_sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    upstream_remote: str | None = None
    push_upstream: bool = False
    validation_timeout_seconds: float = 900.0
    setup_commands: tuple[str, ...] = ()
    external_ci_enabled: bool = False
    external_ci_repo: str | None = None
    external_ci_max_consecutive_errors: int = 5

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
            "upstream": {"remote": self.upstream_remote, "push": self.push_upstream},
            "worker_templates": sorted(self.worker_templates),
            "setup_commands": list(self.setup_commands),
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
    review_policy = normalize_review_policy(str(execution.get("default_review_policy") or "INDEPENDENT").upper())
    if review_policy not in _REVIEW_POLICIES:
        problems.append(f"`execution.default_review_policy` must be one of {_REVIEW_POLICIES}")

    repository = data.get("repository") or {}
    if not isinstance(repository, dict):
        problems.append("`repository` must be a mapping")
        repository = {}

    upstream = data.get("upstream") or {}
    if not isinstance(upstream, dict):
        problems.append("`upstream` must be a mapping")
        upstream = {}
    upstream_remote = str(upstream["remote"]).strip() if upstream.get("remote") else None
    push_upstream = bool(upstream.get("push", False))
    if push_upstream and not upstream_remote:
        problems.append("`upstream.push` needs `upstream.remote`")
    timeout = execution.get("validation_timeout_seconds", 900)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        problems.append("`execution.validation_timeout_seconds` must be a positive number")
        timeout = 900

    raw_setup = execution.get("setup")
    setup_commands: tuple[str, ...] = ()
    if raw_setup is not None:
        if not isinstance(raw_setup, list) or not all(isinstance(item, str) and item.strip() for item in raw_setup):
            problems.append("`execution.setup` must be a list of non-empty command strings")
        else:
            setup_commands = tuple(item.strip() for item in raw_setup)

    external_ci = execution.get("external_ci") or {}
    if not isinstance(external_ci, dict):
        problems.append("`execution.external_ci` must be a mapping")
        external_ci = {}
    external_ci_enabled = bool(external_ci.get("enabled", False))
    external_ci_repo = str(external_ci["repo"]).strip() if external_ci.get("repo") else None
    if external_ci_enabled and not external_ci_repo:
        problems.append("`execution.external_ci.enabled` needs `execution.external_ci.repo`")
    external_ci_max_errors = external_ci.get("max_consecutive_errors", 5)
    if (
        not isinstance(external_ci_max_errors, int)
        or isinstance(external_ci_max_errors, bool)
        or external_ci_max_errors < 1
    ):
        problems.append("`execution.external_ci.max_consecutive_errors` must be a positive integer")
        external_ci_max_errors = 5

    raw_state = str(data.get("state_dir") or ".build-coordinator")
    state_dir = Path(raw_state).expanduser()
    if not state_dir.is_absolute():
        state_dir = root_path / state_dir
    state_dir = state_dir.resolve()

    templates: dict[str, list[dict[str, Any]]] = {}
    workers = data.get("workers") or {}
    if not isinstance(workers, dict):
        problems.append("`workers` must be a mapping of role -> worker template")
        workers = {}
    for role, template in workers.items():
        if role not in _WORKER_ROLES:
            problems.append(f"unknown worker role {role!r}; expected one of {_WORKER_ROLES}")
        else:
            entries = template if isinstance(template, list) else [template]
            if not entries or not all(isinstance(entry, dict) for entry in entries):
                problems.append(f"`workers.{role}` must be a mapping or a list of mappings")
            else:
                templates[role] = [dict(entry) for entry in entries]

    runner_config = data.get("runner_config")
    runner_path: Path | None = None
    if runner_config:
        runner_path = Path(str(runner_config)).expanduser()
        if not runner_path.is_absolute():
            runner_path = (root_path / PROJECT_DIR / runner_path).resolve()
        if not runner_path.is_file():
            problems.append(f"`runner_config` file does not exist: {runner_path}")

    raw_sources = data.get("task_sources")
    sources: dict[str, dict[str, Any]] = {}
    if raw_sources is not None:
        if isinstance(raw_sources, list):
            sources = {str(item): {"enabled": True} for item in raw_sources}
        elif isinstance(raw_sources, dict):
            sources = {str(k): dict(v or {}) for k, v in raw_sources.items()}
        else:
            problems.append("`task_sources` must be a mapping or list of names")

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
        upstream_remote=upstream_remote,
        push_upstream=push_upstream,
        validation_timeout_seconds=float(timeout),
        setup_commands=setup_commands,
        external_ci_enabled=external_ci_enabled,
        external_ci_repo=external_ci_repo,
        external_ci_max_consecutive_errors=int(external_ci_max_errors),
    )


def registry_path() -> Path:
    configured = os.getenv(REGISTRY_ENV)
    if configured:
        return Path(configured).expanduser()
    home = os.getenv("STAGEMESH_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".build-coordinator") / "projects.json"


def unregister_project(query: str | Path) -> list[Path]:
    """Remove registry entries matching a project name/alias or a path."""
    wanted = _fold(str(query))
    keep: list[str] = []
    removed: list[Path] = []
    for root in registered_roots():
        matches = _fold(str(root)) == wanted or str(root).lower() == str(query).lower()
        if not matches:
            try:
                matches = wanted in load_project(root).names()
            except ProjectError:
                matches = False
        (removed if matches else keep).append(root)
    if not removed:
        raise ProjectError(f"no registered project matches {query!r}")
    _write_registry([row for row in _registry_entries() if Path(row["path"]) in {Path(k) for k in keep}])
    return removed


def _registry_entries() -> list[dict[str, Any]]:
    """Registry rows: `{"path": ..., "env": {...}, "path_prepend": [...]}`.

    Older registries stored bare path strings; both forms are read. This file is
    per-user discovery metadata plus machine-local execution environment (for
    example a virtualenv's bin directory); it is never the project backlog."""
    path = registry_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectError(f"project registry {path} is unreadable: {exc}") from exc
    rows = []
    for item in data.get("projects", []):
        row = {"path": str(item)} if isinstance(item, str) else dict(item)
        row.setdefault("env", {})
        row.setdefault("path_prepend", [])
        rows.append(row)
    return rows


def _write_registry(rows: list[dict[str, Any]]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    compact = [
        row["path"] if not row.get("env") and not row.get("path_prepend") else row
        for row in sorted(rows, key=lambda r: r["path"])
    ]
    path.write_text(json.dumps({"projects": compact}, indent=2) + "\n", encoding="utf-8")


def registered_roots() -> list[Path]:
    return [Path(row["path"]) for row in _registry_entries()]


def machine_environment(root: str | Path) -> dict[str, Any]:
    """Machine-local env a registered project should run with (empty if none)."""
    for row in _registry_entries():
        if Path(row["path"]) == Path(root):
            return {"env": dict(row["env"]), "path_prepend": list(row["path_prepend"])}
    return {"env": {}, "path_prepend": []}


def set_machine_environment(
    root: str | Path, *, env: dict[str, str] | None = None, path_prepend: list[str] | None = None
) -> None:
    rows = _registry_entries()
    for row in rows:
        if Path(row["path"]) == Path(root):
            row["env"].update(env or {})
            for entry in path_prepend or []:
                if entry not in row["path_prepend"]:
                    row["path_prepend"].append(entry)
            _write_registry(rows)
            return
    raise ProjectError(f"{root} is not registered")


def register_project(root: str | Path) -> ProjectDefinition:
    project = load_project(root)
    rows = _registry_entries()
    kept = []
    for row in rows:
        try:
            other = load_project(row["path"])
        except ProjectError:
            kept.append(row)
            continue
        if other.root == project.root:
            kept.append(row)  # already registered: keep its machine-local settings
        elif other.project_id != project.project_id:
            kept.append(row)
    if not any(Path(r["path"]) == project.root for r in kept):
        kept.append({"path": str(project.root), "env": {}, "path_prepend": []})
    _write_registry(kept)
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
