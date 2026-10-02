from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar


class ObjectiveValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Objective:
    id: str
    title: str
    tasks: tuple[str, ...]


class ObjectivePlanner:
    """Validates structured planner output before it can affect lifecycle state."""

    REQUIRED_TASK_FIELDS: ClassVar[set[str]] = {"id", "title"}

    def parse(self, payload: str | dict[str, Any]) -> Objective:
        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ObjectiveValidationError("planner output must be valid JSON") from exc
        else:
            data = payload
        if not isinstance(data, dict):
            raise ObjectiveValidationError("planner output must be an object")
        objective_id = _validate_text(data.get("id"), "objective id")
        title = _validate_text(data.get("title"), "objective title")
        tasks = data.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ObjectiveValidationError("objective tasks must be a non-empty list")
        task_ids: list[str] = []
        seen_task_ids: set[str] = set()
        for item in tasks:
            if not isinstance(item, dict) or not self.REQUIRED_TASK_FIELDS <= set(item):
                raise ObjectiveValidationError("each task requires id and title")
            task_id = _validate_text(item["id"], "task id")
            _validate_text(item["title"], "task title")
            if task_id in seen_task_ids:
                raise ObjectiveValidationError(f"duplicate task id: {task_id}")
            seen_task_ids.add(task_id)
            task_ids.append(task_id)
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
        if not isinstance(objective, Objective):
            raise ObjectiveValidationError("objective must be parsed before backlog writing")
        if not isinstance(source_payload, dict):
            raise ObjectiveValidationError("source payload must be an object")
        source_tasks = source_payload.get("tasks")
        if not isinstance(source_tasks, list):
            raise ObjectiveValidationError("source payload tasks must be a list")
        tasks = []
        by_id: dict[str, dict[str, Any]] = {}
        for item in source_tasks:
            if not isinstance(item, dict):
                raise ObjectiveValidationError("source payload task entries must be objects")
            task_id = _validate_text(item.get("id"), "source payload task id")
            by_id[task_id] = item
        for task_id in objective.tasks:
            if task_id not in by_id:
                raise ObjectiveValidationError(f"source payload is missing task: {task_id}")
            item = by_id[task_id]
            eligible = item.get("eligible", True)
            if not isinstance(eligible, bool):
                raise ObjectiveValidationError("source payload task eligible must be a boolean")
            state = item.get("state", "OPEN")
            if not isinstance(state, str) or not state.strip():
                raise ObjectiveValidationError("source payload task state must be a non-empty string")
            dependencies = item.get("dependencies", [])
            if not isinstance(dependencies, list) or not all(isinstance(dep, str) for dep in dependencies):
                raise ObjectiveValidationError("source payload task dependencies must be a list of strings")
            tasks.append(
                {
                    "id": item["id"],
                    "title": _validate_text(item.get("title"), "source payload task title"),
                    "eligible": eligible,
                    "state": state.strip(),
                    "dependencies": dependencies,
                }
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"objective": objective.id, "tasks": tasks}, indent=2), encoding="utf-8")


def _validate_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ObjectiveValidationError(f"{field} is required")
    if len(value.strip()) > 200:
        raise ObjectiveValidationError(f"{field} must be 200 characters or fewer")
    return value.strip()
