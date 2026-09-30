from __future__ import annotations

import hashlib
import json
import re
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


def _sanitize_plan_dict(data: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(data)
    cleaned.pop("chain_of_thought", None)
    cleaned.pop("thinking", None)
    cleaned.pop("scratchpad", None)
    cleaned.pop("model_identity", None)
    return cleaned


def normalize_planner_payload(payload: str | dict[str, Any]) -> dict[str, Any]:
    """Extract and normalize a plan payload from bare dict, markdown fences, or executor envelope."""
    if isinstance(payload, dict):
        if "plan" in payload and isinstance(payload["plan"], dict):
            return _sanitize_plan_dict(payload["plan"])
        return _sanitize_plan_dict(payload)
    if not isinstance(payload, str):
        raise ObjectiveValidationError("planner output must be an object or string")

    text = payload.strip()
    # Check for markdown code blocks (e.g. ```json ... ```)
    fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    candidates: list[str] = [block.strip() for block in fenced if block.strip()]
    if text.startswith("{") and text.endswith("}"):
        candidates.insert(0, text)
    if not candidates:
        candidates = [text]

    for cand in candidates:
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                if "plan" in parsed and isinstance(parsed["plan"], dict):
                    return _sanitize_plan_dict(parsed["plan"])
                return _sanitize_plan_dict(parsed)
        except json.JSONDecodeError:
            continue
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ObjectiveValidationError("planner output must be valid JSON") from exc
    if not isinstance(data, dict):
        raise ObjectiveValidationError("planner output must be an object")
    if "plan" in data and isinstance(data["plan"], dict):
        return _sanitize_plan_dict(data["plan"])
    return _sanitize_plan_dict(data)


def _check_for_cycles(tasks: list[dict[str, Any]]) -> None:
    graph = {item["id"]: set(item.get("dependencies", [])) for item in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, chain: list[str]) -> None:
        if node not in graph:
            return
        if node in visiting:
            cycle = " -> ".join(chain + [node])
            raise ObjectiveValidationError(f"dependency cycle detected: {cycle}")
        if node in visited:
            return
        visiting.add(node)
        for dep in sorted(graph[node]):
            visit(dep, chain + [node])
        visiting.discard(node)
        visited.add(node)

    for task_id in sorted(graph):
        visit(task_id, [])


class ObjectivePlanner:
    """Validates structured planner output before it can affect lifecycle state."""

    REQUIRED_TASK_FIELDS = {"id", "title"}

    def __init__(self, *, max_consecutive_failures: int = 1) -> None:
        self.max_consecutive_failures = max_consecutive_failures
        self._consecutive_failures: dict[str, int] = {}

    def _fingerprint(self, payload: str | dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True) if isinstance(payload, dict) else str(payload)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_suppressed(self, prompt_or_payload: str | dict[str, Any]) -> bool:
        fp = self._fingerprint(prompt_or_payload)
        return self._consecutive_failures.get(fp, 0) >= self.max_consecutive_failures

    def record_failure(self, prompt_or_payload: str | dict[str, Any]) -> None:
        fp = self._fingerprint(prompt_or_payload)
        self._consecutive_failures[fp] = self._consecutive_failures.get(fp, 0) + 1

    def clear_failure(self, prompt_or_payload: str | dict[str, Any]) -> None:
        fp = self._fingerprint(prompt_or_payload)
        self._consecutive_failures.pop(fp, None)

    def parse(self, payload: str | dict[str, Any]) -> Objective:
        if self.is_suppressed(payload):
            raise ObjectiveValidationError("planner retry suppressed: unchanged malformed output")
        try:
            data = normalize_planner_payload(payload)
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
            _check_for_cycles(tasks)
        except Exception:
            self.record_failure(payload)
            raise
        self.clear_failure(payload)
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
