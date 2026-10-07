"""Scope discipline: what a task may touch, what it must not, and the work it explicitly deferred.

A `TaskScope` is derived from the task's frozen change contract (allowed/forbidden files, objective, acceptance criteria) plus a
durable ledger of deferred work. Findings and CI failures that fall outside the scope are recorded as deferred, never fixed
opportunistically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..audit import record_audit
from ..contracts import ChangeContract, _matches
from ..persistence import Store

DEFERRED_EVENT = "autonomy.deferred_work"


@dataclass(frozen=True)
class DeferredItem:
    summary: str
    source: str  # review | ci | scope
    path: str | None = None
    candidate_sha: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"summary": self.summary, "source": self.source, "path": self.path, "candidate_sha": self.candidate_sha}


@dataclass(frozen=True)
class TaskScope:
    objective: str
    acceptance_criteria: tuple[str, ...] = ()
    allowed_files: tuple[str, ...] = ("**",)
    forbidden_files: tuple[str, ...] = ()
    deferred: tuple[DeferredItem, ...] = field(default_factory=tuple)

    @classmethod
    def from_contract(cls, contract: ChangeContract, deferred: tuple[DeferredItem, ...] = ()) -> TaskScope:
        return cls(
            objective=contract.objective,
            acceptance_criteria=contract.acceptance_criteria,
            allowed_files=contract.allowed_files,
            forbidden_files=tuple(contract.forbidden_files) + tuple(contract.exclusions),
            deferred=deferred,
        )

    def permits(self, path: str) -> bool:
        normalized = path.replace("\\", "/")
        return _matches(normalized, self.allowed_files) and not _matches(normalized, self.forbidden_files)

    def out_of_scope(self, paths: list[str] | tuple[str, ...]) -> list[str]:
        return sorted(path for path in paths if not self.permits(path))

    def to_dict(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "allowed_files": list(self.allowed_files),
            "forbidden_files": list(self.forbidden_files),
            "deferred": [item.to_dict() for item in self.deferred],
        }


def record_deferred(store: Store, task_id: str, item: DeferredItem) -> None:
    """Remember work the task deliberately did not do, so it is neither lost nor done opportunistically."""
    record_audit(store, DEFERRED_EVENT, {"task_id": task_id, **item.to_dict()})


def deferred_items(store: Store, task_id: str) -> tuple[DeferredItem, ...]:
    items: list[DeferredItem] = []
    for row in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (DEFERRED_EVENT,)):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if payload.get("task_id") == task_id:
            items.append(DeferredItem(payload["summary"], payload.get("source", "scope"), payload.get("path"), payload.get("candidate_sha")))
    return tuple(items)
