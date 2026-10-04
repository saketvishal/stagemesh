"""`stagemesh regenerate-contracts`: re-plan auto-generated contracts after the scope map changed.

Only contracts StageMesh itself generated (`generated_by: stagemesh-auto-plan`, no project profile) are ever touched; a contract an
operator wrote is never regenerated. The gates of the old contract are kept as they are, only the scope (and the limits that
follow from it) is re-derived from the current scope map. A contract is regenerated only when the new scope is narrow and differs
from the old one: regeneration never widens a contract, and an unbounded task keeps what it has.

For a task that has already started, the new contract goes in through `rebind_contract`, so every safety rule of that command holds
(no active claim or running execution, no done task, no silently invalidated passed evidence) and its audit event is written too;
if it refuses, the contract file is put back exactly as it was. Candidates, evidence, findings and audit rows are never deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .audit import record_audit
from .auto_plan import GENERATED_BY, AutoPlanError, build_contract, derive_task_scope
from .contracts import ContractError, canonical_contract_json, parse_contract, task_contract_path
from .domain import Stage, TaskStatus
from .operator_actions import _operator
from .persistence import Store
from .recovery import RecoveryRefusal, _require_repairable, rebind_contract

REGENERATED_EVENT = "contract.regenerated"
BROAD = frozenset({"**", "*", "**/*"})


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_contract_json(parse_contract(payload)).encode("utf-8")).hexdigest()


def _scope_of(payload: dict[str, Any]) -> dict[str, Any]:
    recorded = payload.get("scope")
    allowed = [str(p) for p in payload.get("allowed_files", [])]
    if isinstance(recorded, dict) and recorded.get("mode") in ("narrow", "broad"):
        return {"mode": recorded["mode"], "allowed_files": allowed, "areas": list(recorded.get("areas", []))}
    # contracts generated before scopes were recorded: judged by what they allow
    return {"mode": "broad" if BROAD & {p.replace("\\", "/") for p in allowed} or not allowed else "narrow", "allowed_files": allowed, "areas": []}


def assess(store: Store, project: Path, task_id: str) -> dict[str, Any]:
    """Read-only: would this task's contract be regenerated, and if not, why? Never raises for ordinary situations."""
    item: dict[str, Any] = {"task_id": task_id, "status": "skipped", "reason": "", "old_scope": None, "new_scope": None, "old_digest": None, "new_digest": None}
    path = task_contract_path(project, task_id)
    if path is None:
        item["reason"] = "no contract file"
        return item
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        item["old_digest"] = _digest(old)
    except (OSError, ValueError, ContractError) as exc:
        item["reason"] = f"contract file is unreadable: {exc}"
        return item
    item["old_scope"] = _scope_of(old)
    if old.get("generated_by") != GENERATED_BY:
        item["reason"] = "hand-written contract; never regenerated"
        return item
    if old.get("profile"):
        item["reason"] = "generated from the project profile, not the scope map"
        return item
    if store.get_task(task_id) is None:
        item["reason"] = "task is not in the store"
        return item
    try:
        decision = derive_task_scope(store, project, task_id)
    except AutoPlanError as exc:
        item["reason"] = exc.message
        return item
    item["new_scope"] = {"mode": decision.mode, "allowed_files": list(decision.allowed_files), "areas": list(decision.areas), "summary": decision.describe()}
    if decision.mode != "narrow":
        item["reason"] = f"no bounded scope for this task now ({decision.reason}); the existing contract is kept"
        return item
    if sorted(item["old_scope"]["allowed_files"]) == sorted(decision.allowed_files):
        item["status"], item["reason"] = "unchanged", "scope already matches the scope map"
        return item
    gates = old.get("required_tests")
    if not gates:
        item["reason"] = "the old contract has no gates to carry over"
        return item
    item["status"], item["reason"] = "would_regenerate", f"scope {item['old_scope']['mode']} -> narrow"
    item["_gates"], item["_decision"], item["_path"] = gates, decision, path
    return item


def recommendation_for(store: Store, project: Path, task_id: str) -> dict[str, Any] | None:
    """The advice `queue-run` prints for a task whose generated contract is broad but could now be narrow, else None."""
    try:
        item = assess(store, project, task_id)
    except Exception:  # noqa: BLE001 - advice must never break a queue run
        return None
    if item["status"] != "would_regenerate" or item["old_scope"]["mode"] != "broad":
        return None
    return {
        "task_id": task_id,
        "old_scope": item["old_scope"]["allowed_files"],
        "new_scope": item["new_scope"]["allowed_files"],
        "command": f"stagemesh regenerate-contracts --task {task_id}",
    }


def _public(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if not k.startswith("_")}


def regenerate_one(store: Store, project: Path, task_id: str, *, dry_run: bool, validate: bool, force: bool, reason: str | None) -> dict[str, Any]:
    item = assess(store, project, task_id)
    if item["status"] != "would_regenerate":
        return _public(item)
    try:
        task = _require_repairable(store, task_id)  # active claim, running execution, done task
    except RecoveryRefusal as exc:
        item.update(status="refused", reason=f"{exc.code}: {exc}")
        return _public(item)
    decision, path = item["_decision"], item["_path"]
    payload = build_contract(store, project, task_id, item["_gates"], decision)
    new_digest = _digest(payload)
    item["new_digest"] = new_digest
    if dry_run:
        return _public(item)
    previous_text = path.read_text(encoding="utf-8")
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)
    started = store.task_contract(task_id) is not None or any(
        store.contract_binding(task_id, str(c["sha"])) for c in store.conn.execute("SELECT sha FROM candidates WHERE task_id=?", (task_id,))
    )
    why = (reason or "").strip() or "regenerated after the scope map changed"
    rebound: dict[str, Any] | None = None
    try:
        if started:
            rebound = rebind_contract(store, project, task_id, validate=validate, reason=why, force=force)
    except RecoveryRefusal as exc:
        path.write_text(previous_text, encoding="utf-8")  # put the operator's tree back exactly as it was
        item.update(status="refused", reason=f"{exc.code}: {exc}", new_digest=None)
        return _public(item)
    record_audit(
        store,
        REGENERATED_EVENT,
        {
            "task_id": task_id,
            "path": str(path),
            "old_scope": item["old_scope"],
            "new_scope": item["new_scope"],
            "old_digest": item["old_digest"],
            "new_digest": new_digest,
            "rebound": rebound is not None,
            "validated": bool(rebound and rebound.get("validation") and rebound["validation"].get("ran")),
            "reason": why,
            "operator": _operator(),
            "operator_action": "REGENERATE_CONTRACT",
        },
    )
    item.update(status="regenerated", reason=why, rebound=rebound is not None)
    if rebound is not None:
        item["validation"] = rebound.get("validation")
        item["stage"], item["task_status"] = rebound["stage"], rebound["status"]
    return _public(item)


def regenerate_contracts(
    store: Store, project: Path, task_id: str | None = None, *, dry_run: bool = False, validate: bool = False, force: bool = False, reason: str | None = None
) -> list[dict[str, Any]]:
    """Regenerate every eligible auto-generated contract (or just `task_id`'s). One result per examined task."""
    if task_id is not None:
        if store.get_task(task_id) is None:
            raise RecoveryRefusal("unknown_task", f"task does not exist: {task_id}")
        return [regenerate_one(store, project, task_id, dry_run=dry_run, validate=validate, force=force, reason=reason)]
    results = []
    for row in store.tasks():
        if row["status"] == TaskStatus.DONE or row["stage"] == Stage.DONE:
            continue
        if task_contract_path(project, str(row["id"])) is None:
            continue
        results.append(regenerate_one(store, project, str(row["id"]), dry_run=dry_run, validate=validate, force=force, reason=reason))
    return results


def format_results(results: list[dict[str, Any]], dry_run: bool) -> str:
    if not results:
        return "no contracts to examine"
    lines = []
    for r in results:
        old = r["old_scope"]["allowed_files"] if r.get("old_scope") else None
        new = r["new_scope"]["allowed_files"] if r.get("new_scope") else None
        status = {"would_regenerate": "WOULD REGENERATE", "regenerated": "regenerated"}.get(r["status"], r["status"])
        lines.append(f"task {r['task_id']}: {status}" + (f" - {r['reason']}" if r["reason"] else ""))
        if r["status"] in ("would_regenerate", "regenerated"):
            lines.append(f"  old scope: {', '.join(old or [])}  (digest {str(r['old_digest'])[:12]})")
            lines.append(f"  new scope: {', '.join(new or [])}  (digest {str(r['new_digest'])[:12]})")
            check = r.get("validation")
            if check:
                lines.append("  validation: " + (f"{check['status']}" + (" - advanced" if check["advanced"] else "") if check["ran"] else f"not run ({check['reason']})"))
    if dry_run:
        lines.append("dry run: nothing was changed")
    return "\n".join(lines)
