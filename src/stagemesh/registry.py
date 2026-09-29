from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class RegistryConflictError(ValueError):
    pass


class RegistryValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectRegistration:
    name: str
    path: Path
    db_path: Path


class GlobalRegistry:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    def _normalize(self, project: ProjectRegistration) -> ProjectRegistration:
        if not isinstance(project, ProjectRegistration):
            raise RegistryValidationError("registry project must be a ProjectRegistration")
        name = project.name.strip()
        if not name:
            raise RegistryValidationError("project name is required")
        path = project.path.resolve()
        db_path = project.db_path.resolve()
        try:
            db_path.relative_to(path)
        except ValueError as exc:
            raise RegistryValidationError("registry db_path must be inside the project path") from exc
        return ProjectRegistration(name=name, path=path, db_path=db_path)

    def load(self) -> list[ProjectRegistration]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RegistryValidationError("registry file must be valid JSON") from exc
        if not isinstance(data, dict):
            raise RegistryValidationError("registry root must be an object")
        projects = data.get("projects", [])
        if not isinstance(projects, list):
            raise RegistryValidationError("registry projects must be a list")
        loaded: list[ProjectRegistration] = []
        names: set[str] = set()
        paths: set[Path] = set()
        for item in projects:
            if not isinstance(item, dict):
                raise RegistryValidationError("registry project entries must be objects")
            if not all(isinstance(item.get(key), str) and item.get(key) for key in ("name", "path", "db_path")):
                raise RegistryValidationError("registry project entries require name, path, and db_path")
            project = self._normalize(
                ProjectRegistration(
                    name=str(item["name"]),
                    path=Path(item["path"]),
                    db_path=Path(item["db_path"]),
                )
            )
            if project.name in names:
                raise RegistryValidationError(f"duplicate registry project name: {project.name}")
            if project.path in paths:
                raise RegistryValidationError(f"duplicate registry project path: {project.path}")
            names.add(project.name)
            paths.add(project.path)
            loaded.append(project)
        return loaded

    def save(self, projects: list[ProjectRegistration]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        normalized = [self._normalize(project) for project in projects]
        self.path.write_text(
            json.dumps(
                {
                    "projects": [
                        {"name": item.name, "path": str(item.path), "db_path": str(item.db_path)}
                        for item in sorted(normalized, key=lambda item: item.name)
                    ]
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def register(self, project: ProjectRegistration) -> None:
        normalized = self._normalize(project)
        projects = self.load()
        for item in projects:
            if item.name == normalized.name and item.path != normalized.path:
                raise RegistryConflictError(f"project name already registered for a different path: {normalized.name}")
            if item.path == normalized.path and item.name != normalized.name:
                raise RegistryConflictError(f"project path already registered under a different name: {normalized.path}")
        projects = [item for item in projects if item.name != normalized.name]
        projects.append(normalized)
        self.save(projects)
