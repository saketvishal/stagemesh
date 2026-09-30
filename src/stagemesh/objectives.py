from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .persistence import Store


class ObjectiveValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Objective:
    id: str
    title: str
    tasks: tuple[str, ...]


def _sanitize_plan_dict(data: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(data)
    # Strip any scratchpads, thinking, model identity, or untrusted lifecycle identity
    cleaned.pop("chain_of_thought", None)
    cleaned.pop("thinking", None)
    cleaned.pop("scratchpad", None)
    cleaned.pop("model_identity", None)
    cleaned.pop("role", None)
    cleaned.pop("status", None)
    cleaned.pop("execution_id", None)
    cleaned.pop("task_id", None)
    cleaned.pop("schema_version", None)

    # Also clean tasks inside if present
    if "tasks" in cleaned and isinstance(cleaned["tasks"], list):
        cleaned_tasks = []
        for task in cleaned["tasks"]:
            if isinstance(task, dict):
                t_copy = dict(task)
                t_copy.pop("chain_of_thought", None)
                t_copy.pop("thinking", None)
                t_copy.pop("scratchpad", None)
                cleaned_tasks.append(t_copy)
            else:
                cleaned_tasks.append(task)
        cleaned["tasks"] = cleaned_tasks

    return cleaned


def parse_planner_envelope(payload: str | dict[str, Any]) -> dict[str, Any]:
    """Extract and sanitize only the plan payload from a full executor envelope.

    Legacy parity (build_coordinator/agents/wrapper.py):
    Requires a full executor envelope containing a top-level 'plan' mapping.
    Rejects bare ObjectivePlan objects without an envelope.
    """
    candidates: list[dict[str, Any]] = []

    if isinstance(payload, dict):
        candidates.append(payload)
    elif isinstance(payload, str):
        text = payload.strip()
        fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
        for block in fenced[::-1]:
            candidate = block.strip()
            if candidate.startswith("{") and candidate.endswith("}"):
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        candidates.append(parsed)
                except json.JSONDecodeError:
                    pass
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    candidates.append(parsed)
            except json.JSONDecodeError:
                pass
    else:
        raise ObjectiveValidationError("planner output must be an object or string")

    for cand in candidates:
        plan = cand.get("plan")
        if isinstance(plan, dict):
            return _sanitize_plan_dict(plan)

    raise ObjectiveValidationError(
        "planner provider output requires full envelope containing 'plan' mapping; bare plan rejected"
    )


def normalize_user_plan_payload(payload: str | dict[str, Any]) -> dict[str, Any]:
    """Extract and normalize a plan payload from user-authored plan file (bare plan or envelope)."""
    if isinstance(payload, dict):
        if "plan" in payload and isinstance(payload["plan"], dict):
            return _sanitize_plan_dict(payload["plan"])
        return _sanitize_plan_dict(payload)
    if not isinstance(payload, str):
        raise ObjectiveValidationError("planner output must be an object or string")

    text = payload.strip()
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


# Backward-compatible alias
normalize_planner_payload = normalize_user_plan_payload


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

    def __init__(self, store: Store | None = None, *, max_consecutive_failures: int = 1) -> None:
        self.store = store
        self.max_consecutive_failures = max_consecutive_failures
        self._in_memory_failures: dict[str, tuple[int, str]] = {}

    def _contract_hash(self, contract: str | dict[str, Any] | None) -> str:
        if contract is None:
            return ""
        raw = json.dumps(contract, sort_keys=True) if isinstance(contract, dict) else str(contract)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_suppressed(self, target_key: str, contract: str | dict[str, Any] | None = None) -> bool:
        chash = self._contract_hash(contract)
        if self.store is not None:
            key = f"planner:{target_key}"
            state = self.store.get_retry_state(key)
            if state is not None and state["attempts"] >= self.max_consecutive_failures:
                # If reason matches current contract hash, it stays suppressed
                # If contract hash has changed, allow recovery!
                if chash and state["reason"] != chash:
                    self.store.clear_retry_state(key)
                    return False
                return True
            return False
        # In-memory fallback
        count, saved_hash = self._in_memory_failures.get(target_key, (0, ""))
        if count >= self.max_consecutive_failures:
            if chash and saved_hash != chash:
                self._in_memory_failures.pop(target_key, None)
                return False
            return True
        return False

    def record_failure(self, target_key: str, contract: str | dict[str, Any] | None = None, reason: str | None = None) -> None:
        chash = self._contract_hash(contract) or (reason or "malformed_output")
        if self.store is not None:
            from .retry import RetryRegistry
            reg = RetryRegistry(self.store)
            reg.record_failure(f"planner:{target_key}", chash)
            return
        count, _ = self._in_memory_failures.get(target_key, (0, ""))
        self._in_memory_failures[target_key] = (count + 1, chash)

    def record_success(self, target_key: str) -> None:
        if self.store is not None:
            from .retry import RetryRegistry
            reg = RetryRegistry(self.store)
            reg.record_success(f"planner:{target_key}")
            return
        self._in_memory_failures.pop(target_key, None)

    def parse_user_plan(self, payload: str | dict[str, Any]) -> tuple[Objective, dict[str, Any]]:
        """Parse user-authored plan, validate schema, and return (Objective, sanitized_payload)."""
        data = normalize_user_plan_payload(payload)
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
        return Objective(objective_id, title, tuple(task_ids)), data

    def parse_provider_plan(
        self,
        payload: str | dict[str, Any],
        *,
        contract: str | dict[str, Any] | None = None,
        target_key: str | None = None,
    ) -> tuple[Objective, dict[str, Any]]:
        """Parse provider planner output requiring full envelope. Rejects bare ObjectivePlan."""
        key = target_key or ("provider:" + self._contract_hash(contract or payload))
        effective_contract = contract if contract is not None else payload
        if self.is_suppressed(key, effective_contract):
            raise ObjectiveValidationError("planner retry suppressed: unchanged malformed output")
        try:
            plan_data = parse_planner_envelope(payload)
            obj, sanitized_plan = self.parse_user_plan(plan_data)
        except Exception:
            self.record_failure(key, effective_contract)
            raise
        self.record_success(key)
        return obj, sanitized_plan

    def parse(
        self,
        payload: str | dict[str, Any],
        *,
        contract: str | dict[str, Any] | None = None,
        target_key: str | None = None,
    ) -> Objective:
        key = target_key or ("anonymous:" + self._contract_hash(contract or payload))
        effective_contract = contract if contract is not None else payload
        if self.is_suppressed(key, effective_contract):
            raise ObjectiveValidationError("planner retry suppressed: unchanged malformed output")
        try:
            obj, _ = self.parse_user_plan(payload)
        except Exception:
            self.record_failure(key, effective_contract)
            raise
        self.record_success(key)
        return obj

    def build_backlog_data(self, objective: Objective, source_payload: dict[str, Any]) -> dict[str, Any]:
        """Validate and construct serializable backlog data without persistent mutations."""
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
        return {"objective": objective.id, "tasks": tasks}

    def write_backlog(self, objective: Objective, source_payload: dict[str, Any], path: Path) -> None:
        data = self.build_backlog_data(objective, source_payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _validate_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ObjectiveValidationError(f"{field} is required")
    if len(value.strip()) > 200:
        raise ObjectiveValidationError(f"{field} must be 200 characters or fewer")
    return value.strip()
