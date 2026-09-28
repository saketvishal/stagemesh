from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class RegistryConflictError(ValueError):
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
        name = project.name.strip()
        if not name:
            raise ValueError("project name is required")
        return ProjectRegistration(name=name, path=project.path.resolve(), db_path=project.db_path.resolve())

    def load(self) -> list[ProjectRegistration]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        projects = data.get("projects", [])
        if not isinstance(projects, list):
            raise ValueError("registry projects must be a list")
        loaded: list[ProjectRegistration] = []
        for item in projects:
            if not isinstance(item, dict):
                raise ValueError("registry project entries must be objects")
            loaded.append(
                self._normalize(
                    ProjectRegistration(
                        name=str(item["name"]),
                        path=Path(item["path"]),
                        db_path=Path(item["db_path"]),
                    )
                )
            )
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
