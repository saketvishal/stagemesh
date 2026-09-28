from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectRegistration:
    name: str
    path: Path
    db_path: Path


class GlobalRegistry:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[ProjectRegistration]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [
            ProjectRegistration(
                name=str(item["name"]),
                path=Path(item["path"]),
                db_path=Path(item["db_path"]),
            )
            for item in data.get("projects", [])
        ]

    def save(self, projects: list[ProjectRegistration]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "projects": [
                        {"name": item.name, "path": str(item.path), "db_path": str(item.db_path)}
                        for item in projects
                    ]
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def register(self, project: ProjectRegistration) -> None:
        projects = [item for item in self.load() if item.name != project.name]
        projects.append(project)
        self.save(sorted(projects, key=lambda item: item.name))
