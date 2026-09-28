from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ObjectiveValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Objective:
    id: str
    title: str
    tasks: tuple[str, ...]


class ObjectivePlanner:
    """Validates structured planner output before it can affect lifecycle state."""

    REQUIRED_TASK_FIELDS = {"id", "title"}

    def parse(self, payload: str | dict[str, Any]) -> Objective:
        data = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(data, dict):
            raise ObjectiveValidationError("planner output must be an object")
        objective_id = data.get("id")
        title = data.get("title")
        tasks = data.get("tasks")
        if not isinstance(objective_id, str) or not objective_id:
            raise ObjectiveValidationError("objective id is required")
        if not isinstance(title, str) or not title:
            raise ObjectiveValidationError("objective title is required")
        if not isinstance(tasks, list) or not tasks:
            raise ObjectiveValidationError("objective tasks must be a non-empty list")
        task_ids: list[str] = []
        seen_task_ids: set[str] = set()
        for item in tasks:
            if not isinstance(item, dict) or not self.REQUIRED_TASK_FIELDS <= set(item):
                raise ObjectiveValidationError("each task requires id and title")
            if not isinstance(item["id"], str) or not isinstance(item["title"], str):
                raise ObjectiveValidationError("task id and title must be strings")
            if item["id"] in seen_task_ids:
                raise ObjectiveValidationError(f"duplicate task id: {item['id']}")
            seen_task_ids.add(item["id"])
            task_ids.append(item["id"])
        for item in tasks:
            dependencies = item.get("dependencies", [])
            if not isinstance(dependencies, list):
                raise ObjectiveValidationError("task dependencies must be a list")
            for dependency in dependencies:
                if not isinstance(dependency, str):
                    raise ObjectiveValidationError("task dependency ids must be strings")
                if dependency not in seen_task_ids:
                    raise ObjectiveValidationError(f"unknown dependency: {dependency}")
        return Objective(objective_id, title, tuple(task_ids))

    def write_backlog(self, objective: Objective, source_payload: dict[str, Any], path: Path) -> None:
        tasks = []
        by_id = {str(item["id"]): item for item in source_payload["tasks"]}
        for task_id in objective.tasks:
            item = by_id[task_id]
            tasks.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "eligible": bool(item.get("eligible", True)),
                    "state": str(item.get("state", "OPEN")),
                    "dependencies": list(item.get("dependencies", [])),
                }
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"objective": objective.id, "tasks": tasks}, indent=2), encoding="utf-8")
