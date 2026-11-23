"""Why a task is BLOCKED, as one of four kinds, and which of them StageMesh reconsiders by itself.

A BLOCKED task used to mean "an operator has to look". Most blocks are not that. The kind says who can unblock the task:

* `BLOCKED_HUMAN_DECISION`       a product, spec, security or legal choice that repository state cannot settle (needs a person);
* `BLOCKED_EXTERNAL_DEPENDENCY`  something outside StageMesh must exist first (a missing integration branch, a dependency);
* `BLOCKED_RECOVERABLE_OPERATIONAL` an ordinary engineering or runtime failure whose automatic recovery budget ran out; the task is
                                 reconsidered by `run-window` after the runtime or its configuration changed (bounded);
* `BLOCKED_POLICY_SAFETY`        a safety rule stopped the work (foreign mutation of a workspace or ref); never retried automatically.

New blocks record their kind (`task.block_classified`). Blocks recorded before that existed are classified from the audit events that
caused them, so the tasks that are blocked today are covered without a migration. A block with no recorded cause is left alone.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .audit import record_audit
from .diagnosis import CONTRACT_SCOPE, _last_reset
from .domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from .persistence import Store
from .recovery_decision import RecoveryAction, RecoveryDecision
from .workspace_guard import EXTERNAL_WORKSPACE_MUTATION

BLOCK_EVENT = "task.block_classified"
REENTER_EVENT = "task.block_reentered"
BROWSER_EVIDENCE_UPGRADE_EVENT = "task.browser_evidence_upgrade_reentered"
BROWSER_EVIDENCE_EXPANSION_REENTRY_EVENT = "task.browser_evidence_expansion_reentered"
REVIEW_LIFECYCLE_REPAIR_EVENT = "task.review_lifecycle_repaired"
MAX_REENTRIES = 3


class BlockKind(StrEnum):
    HUMAN_DECISION = "BLOCKED_HUMAN_DECISION"
    EXTERNAL_DEPENDENCY = "BLOCKED_EXTERNAL_DEPENDENCY"
    RECOVERABLE_OPERATIONAL = "BLOCKED_RECOVERABLE_OPERATIONAL"
    POLICY_SAFETY = "BLOCKED_POLICY_SAFETY"


@dataclass(frozen=True)
class BlockInfo:
    kind: BlockKind
    reason: str
    source: str  # recorded | legacy:<event type>
    runtime: str | None = None  # runtime fingerprint when it was blocked; None for blocks that predate the fingerprint
    stage: str | None = None
    candidate_sha: str | None = None
    failure_code: str | None = None
    requirements: tuple[str, ...] = ()


def block_kind_for(decision: RecoveryDecision) -> BlockKind:
    """The kind a blocking recovery decision stands for."""
    if decision.action is RecoveryAction.BLOCK_HUMAN_DECISION:
        return BlockKind.HUMAN_DECISION
    if decision.action is RecoveryAction.ABORT_UNSAFE:
        return BlockKind.POLICY_SAFETY
    if decision.action is RecoveryAction.BLOCK_EXTERNAL:
        return BlockKind.EXTERNAL_DEPENDENCY if decision.external_dependency else BlockKind.RECOVERABLE_OPERATIONAL
    raise ValueError(f"{decision.action} does not block a task")


def runtime_fingerprint(project: Path) -> str:
    """A short hash of what a recovery decision depends on: the StageMesh code that is running, the project's StageMesh configuration and the `python` gates run with."""
    import stagemesh

    digest = hashlib.sha256()
    package = Path(stagemesh.__file__).resolve().parent
    for source in sorted(package.rglob("*.py")):
        digest.update(source.relative_to(package).as_posix().encode())
        digest.update(source.read_bytes())
    digest.update(str(shutil.which("python") or "").encode())  # validation gates run `python` from PATH: a different interpreter is a different environment
    for name in ("config.json", "project.yaml"):
        config = Path(project) / ".stagemesh" / name
        if config.is_file():
            digest.update(config.read_bytes())
    return digest.hexdigest()[:16]


def record_block(
    store: Store,
    task_id: str,
    kind: BlockKind,
    reason: str,
    *,
    stage: str | None = None,
    candidate_sha: str | None = None,
    runtime: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Block the task and record which kind of block it is and exactly why further automatic action stops."""
    store.block_task(task_id)
    record_audit(
        store,
        BLOCK_EVENT,
        {
            "task_id": task_id,
            "kind": kind.value,
            "reason": reason[:1500],
            "stage": stage,
            "candidate_sha": candidate_sha,
            "runtime": runtime,
            **(detail or {}),
        },
    )


def classify_block(store: Store, task_id: str) -> BlockInfo | None:
    """The kind of the task's current block, or None when its cause was not recorded (such a block is never retried automatically)."""
    reset = _last_reset(store, task_id)
    rows = store.conn.execute(
        "SELECT event_type, payload FROM audit_events WHERE event_type IN (?, 'task.remediation_exhausted', 'task.diagnosis_stop', 'task.blocked', 'task.source_retired') "
        "AND created_at>=? AND json_valid(payload) "
        "AND CAST(json_extract(payload, '$.task_id') AS TEXT)=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 300",
        (BLOCK_EVENT, reset, task_id),
    )
    mine = [(str(row["event_type"]), _loads(row["payload"])) for row in rows]
    mine = [(event, payload) for event, payload in mine if payload.get("task_id") == task_id]
    for index, (event, payload) in enumerate(mine):
        if event == BLOCK_EVENT:
            try:
                kind = BlockKind(str(payload.get("kind")))
            except ValueError:
                return None
            reason = str(payload.get("reason", ""))
            requirements = payload.get("requirements")
            recorded = BlockInfo(
                kind=kind,
                reason=reason,
                source="recorded",
                runtime=payload.get("runtime"),
                stage=str(payload["stage"]) if payload.get("stage") else None,
                candidate_sha=str(payload["candidate_sha"]) if payload.get("candidate_sha") else None,
                failure_code=str(payload["failure_code"]) if payload.get("failure_code") else None,
                requirements=tuple(str(item) for item in requirements if str(item).strip())
                if isinstance(requirements, list)
                else (),
            )
            if kind is BlockKind.EXTERNAL_DEPENDENCY and payload.get("failure_code") == "validation_evidence_unconfigured":
                return BlockInfo(
                    BlockKind.RECOVERABLE_OPERATIONAL,
                    f"{reason} [reclassified: review and validation evidence must be reconciled by the current runtime]",
                    "reclassified",
                    payload.get("runtime"),
                    recorded.stage,
                    recorded.candidate_sha,
                    recorded.failure_code,
                    recorded.requirements,
                )
            if (
                kind is BlockKind.EXTERNAL_DEPENDENCY
                and payload.get("failure_code") == "external_evidence_required"
                and (
                    _browser_evidence_upgrade(store, task_id, recorded) is not None
                    or _browser_evidence_expansion(store, task_id, recorded) is not None
                )
            ):
                return BlockInfo(
                    BlockKind.RECOVERABLE_OPERATIONAL,
                    f"{reason} [reclassified: StageMesh can collect or review the missing bounded headless evidence]",
                    "reclassified",
                    recorded.runtime,
                    recorded.stage,
                    recorded.candidate_sha,
                    recorded.failure_code,
                    recorded.requirements,
                )
            if (
                kind is BlockKind.EXTERNAL_DEPENDENCY
                and payload.get("failure_code") == "browser_viewport_proof_required"
                and recorded.candidate_sha
            ):
                from .browser_evidence import invalidated_browser_recapture_recipe

                if invalidated_browser_recapture_recipe(
                    store, task_id, recorded.candidate_sha
                ) is not None:
                    return BlockInfo(
                        BlockKind.RECOVERABLE_OPERATIONAL,
                        f"{reason} [reclassified: StageMesh can replay the recorded loopback capture recipe]",
                        "reclassified",
                        recorded.runtime,
                        recorded.stage,
                        recorded.candidate_sha,
                        recorded.failure_code,
                        recorded.requirements,
                    )
            if (
                kind is BlockKind.HUMAN_DECISION
                and payload.get("failure_code") == "browser_evidence_decision_required"
                and recorded.candidate_sha
            ):
                return BlockInfo(
                    BlockKind.RECOVERABLE_OPERATIONAL,
                    f"{reason} [reclassified: verified captures now continue to independent technical review]",
                    "reclassified",
                    recorded.runtime,
                    recorded.stage,
                    recorded.candidate_sha,
                    recorded.failure_code,
                    recorded.requirements,
                )
            if kind is BlockKind.HUMAN_DECISION and "(judgment_required)" in reason and payload.get("candidate_sha"):
                # a decision inferred from finding wording is re-derived under today's markers: wording that no longer counts is a defect to repair
                from .remediation_recovery import judgment_reason

                open_findings = store.open_findings_for_candidate(task_id, str(payload["candidate_sha"]))
                if open_findings and judgment_reason([str(f["message"]) for f in open_findings]) is None:
                    return BlockInfo(
                        BlockKind.RECOVERABLE_OPERATIONAL,
                        f"{reason} [reclassified: the findings no longer state a decision]",
                        "reclassified",
                        recorded.runtime,
                        recorded.stage,
                        recorded.candidate_sha,
                        recorded.failure_code,
                        recorded.requirements,
                    )
            return recorded
        if event == "task.remediation_exhausted" and payload.get("reason") == "repeated_failure_diagnosed":
            # the diagnosis that caused this stop was recorded just before it and says what kind of failure repeated
            for earlier_event, earlier in mine[index + 1 :]:
                if earlier_event == "task.diagnosis_stop":
                    return _legacy(earlier_event, earlier)
        return _legacy(event, payload)
    return None


def _legacy(event: str, payload: dict[str, Any]) -> BlockInfo:
    """Blocks written before kinds were recorded: remediation budgets and integration states are operational, scope and safety are not."""
    if event == "task.source_retired":
        reason = str(payload.get("reason") or "source retired")
        if payload.get("objective_root") or "objective root" in reason:
            return BlockInfo(BlockKind.HUMAN_DECISION, reason, f"legacy:{event}")
        return BlockInfo(BlockKind.EXTERNAL_DEPENDENCY, reason, f"legacy:{event}")
    if event == "task.blocked":
        reason = str(payload.get("reason", "blocked by a safety rule"))
        if reason == EXTERNAL_WORKSPACE_MUTATION:
            return BlockInfo(BlockKind.POLICY_SAFETY, "a workspace was changed outside StageMesh", f"legacy:{event}")
        lower = reason.lower()
        if any(
            term in lower
            for term in (
                "integration_ref_state",
                "stale_baseline",
                "stale integration",
                "candidate_environment_unavailable",
                "environment",
                "provider_unavailable",
                "transient_provider_failure",
                "quota",
                "timeout",
                "stale_claim",
                "stale_execution",
                "expired_claim",
                "dead_execution",
                "task_synchronization",
                "source_sync",
            )
        ):
            return BlockInfo(BlockKind.RECOVERABLE_OPERATIONAL, reason, f"legacy:{event}")
        return BlockInfo(BlockKind.POLICY_SAFETY, reason, f"legacy:{event}")
    if event == "task.diagnosis_stop":
        if payload.get("category") == CONTRACT_SCOPE:
            return BlockInfo(BlockKind.HUMAN_DECISION, "the contract's allowed files and the work disagree: should the contract be widened (stagemesh rebind-contract) to include what the work needed, or must the work stay inside it?", f"legacy:{event}")
        return BlockInfo(BlockKind.RECOVERABLE_OPERATIONAL, f"stopped on a diagnosed {payload.get('category', 'failure')}", f"legacy:{event}")
    reason = str(payload.get("reason") or "remediation budget exhausted")
    return BlockInfo(BlockKind.RECOVERABLE_OPERATIONAL, reason, f"legacy:{event}")


def reentries_used(store: Store, task_id: str) -> int:
    """Automatic re-entries since an operator last gave the task a fresh budget (an unblock StageMesh did not make itself)."""
    ours = [
        float(row["created_at"])
        for row in store.conn.execute("SELECT payload, created_at FROM audit_events WHERE event_type=?", (REENTER_EVENT,))
        if _loads(row["payload"]).get("task_id") == task_id
    ]
    manual = 0.0
    for row in store.conn.execute("SELECT payload, created_at FROM audit_events WHERE event_type='task.unblocked'"):
        if _loads(row["payload"]).get("task_id") != task_id:
            continue
        at = float(row["created_at"])
        if not any(at <= when <= at + 5.0 for when in ours):  # our own re-entries record theirs right after the unblock
            manual = max(manual, at)
    return sum(1 for when in ours if when > manual)


def _last_reentry_runtime(store: Store, task_id: str) -> str | None:
    for row in store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? AND json_valid(payload) "
        "AND CAST(json_extract(payload, '$.task_id') AS TEXT)=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (REENTER_EVENT, task_id),
    ):
        payload = _loads(row["payload"])
        return payload.get("runtime")
    return None


def _browser_evidence_upgrade(
    store: Store, task_id: str, info: BlockInfo
) -> dict[str, object] | None:
    """The exact new capture that may cross one legacy blocked-state budget."""
    if (
        info.failure_code != "external_evidence_required"
        or not info.candidate_sha
    ):
        return None
    from .browser_evidence import browser_evidence_recheck_requirement
    from .review_disposition import unresolved_adverse_review_findings

    project = (
        store.db_path.parent.parent
        if store.db_path.parent.name == ".stagemesh"
        else store.db_path.parent
    )
    pending = unresolved_adverse_review_findings(
        store, task_id, info.candidate_sha
    )
    requirement = browser_evidence_recheck_requirement(
        store, project, task_id, info.candidate_sha, pending
    )
    if requirement is None:
        return None
    capture_id = requirement.get("capture_id")
    for row in store.task_audit_events(BROWSER_EVIDENCE_UPGRADE_EVENT, task_id):
        payload = _loads(row["payload"])
        if (
            payload.get("candidate_sha") == info.candidate_sha
            and payload.get("capture_id") == capture_id
        ):
            return None
    return requirement


def _browser_evidence_expansion(
    store: Store, task_id: str, info: BlockInfo
) -> Any | None:
    """The retained recipe that can safely collect deeper same-origin UI states."""
    if info.failure_code != "external_evidence_required" or not info.candidate_sha:
        return None
    from .browser_evidence import browser_evidence_expansion_recipe

    project = (
        store.db_path.parent.parent
        if store.db_path.parent.name == ".stagemesh"
        else store.db_path.parent
    )
    recipe = browser_evidence_expansion_recipe(
        store,
        project,
        task_id,
        info.candidate_sha,
        info.requirements,
    )
    if recipe is None:
        return None
    for row in store.task_audit_events(BROWSER_EVIDENCE_EXPANSION_REENTRY_EVENT, task_id):
        payload = _loads(row["payload"])
        if (
            payload.get("candidate_sha") == info.candidate_sha
            and payload.get("source_capture_id") == recipe.source_capture_id
            and payload.get("finding_digest") == recipe.finding_digest
        ):
            return None
    return recipe


def _review_lifecycle_repair(
    store: Store, task_id: str, info: BlockInfo
) -> dict[str, object] | None:
    """Recognize the 0.6.49 false step-cap block after a normal review recheck passed.

    The bad state is deliberately narrow: the task was sent from REVIEW back to
    IMPLEMENT by a remediation created *after* a newer independent normal PASS,
    while the distinct adverse role has not run.  Existing candidate, evidence
    and review history are preserved; only the false remediation handoff is
    undone.
    """
    task = store.get_task(task_id)
    if (
        info.failure_code != "step_cap_no_progress"
        or info.candidate_sha is None
        or task is None
        or task["status"] != TaskStatus.BLOCKED
        or task["stage"] != Stage.IMPLEMENT
    ):
        return None
    remediation = store.latest_task_remediation(task_id)
    if (
        remediation is None
        or str(remediation["stage"]) != str(Stage.REVIEW)
        or str(remediation["candidate_sha"]) != info.candidate_sha
    ):
        return None
    binding = store.contract_binding(task_id, info.candidate_sha)
    if binding is None:
        return None
    from .review import (
        ROLE_ADVERSE,
        ROLE_NORMAL,
        independent_review_verified,
        latest_review_role_evidence,
    )

    digest = str(binding["digest"])
    all_normal = store.bound_evidence_payloads(
        task_id,
        info.candidate_sha,
        EvidenceKind.REVIEW,
        digest,
        EvidenceStatus.FAILED,
    )
    policy_digests = {
        str(payload.get("adverse_policy_digest"))
        for payload in all_normal
        if payload.get("review_role") == ROLE_NORMAL
        and isinstance(payload.get("adverse_policy_digest"), str)
    }
    if not policy_digests:
        return None
    # A single candidate can only be repaired for the policy carried by its
    # newest successful normal role.  Changed policies require fresh review.
    current = latest_review_role_evidence(
        store,
        task_id,
        info.candidate_sha,
        digest,
    )
    normal = current.get(ROLE_NORMAL)
    if normal is None or normal.get("status") != EvidenceStatus.PASSED:
        return None
    payload = normal.get("payload")
    open_findings = store.open_findings_for_candidate(task_id, info.candidate_sha)
    synthetic_finding_id = f"{info.candidate_sha}:failed-{Stage.REVIEW}"
    unexpected_findings = [
        finding
        for finding in open_findings
        if not (
            str(finding["id"]) == synthetic_finding_id
            and str(finding["message"]) == "REVIEW failed without structured findings"
            and float(finding["created_at"]) >= float(normal.get("created_at") or 0.0)
        )
    ]
    if (
        not isinstance(payload, dict)
        or not independent_review_verified(payload)
        or payload.get("adverse_policy_digest") not in policy_digests
        or ROLE_ADVERSE in current
        or float(remediation["created_at"]) < float(normal.get("created_at") or 0.0)
        or unexpected_findings
    ):
        return None
    return {
        "candidate_sha": info.candidate_sha,
        "contract_hash": digest,
        "normal_review_evidence_id": normal["evidence_id"],
        "adverse_policy_digest": payload["adverse_policy_digest"],
        "remediation_id": str(remediation["id"]),
        "synthetic_finding_id": synthetic_finding_id if open_findings else None,
    }


def reentry_refusal(store: Store, task_id: str, fingerprint: str) -> str | None:
    """Why a BLOCKED task is not reconsidered right now, or None when it is eligible."""
    task = store.get_task(task_id)
    if task is None or task["status"] != TaskStatus.BLOCKED:
        return "not blocked"
    info = classify_block(store, task_id)
    if info is None:
        return "blocked with no recorded cause; not retried automatically"
    if info.kind is not BlockKind.RECOVERABLE_OPERATIONAL:
        return f"{info.kind.value}: {info.reason}"
    if store.has_active_claim(task_id) or any(row["task_id"] == task_id for row in store.running_executions()):
        return "has an active claim or running execution"
    if _review_lifecycle_repair(store, task_id, info) is not None:
        return None
    if _browser_evidence_upgrade(store, task_id, info) is not None:
        return None
    if _browser_evidence_expansion(store, task_id, info) is not None:
        return None
    used = reentries_used(store, task_id)
    if used >= MAX_REENTRIES:
        return f"{used} automatic re-entries are spent (limit {MAX_REENTRIES}); `retry-task` grants a fresh budget"
    if fingerprint in (info.runtime, _last_reentry_runtime(store, task_id)):
        return "the runtime and configuration are unchanged since the last attempt; waiting for a change"
    return None


def reenter_blocked_task(store: Store, task_id: str, fingerprint: str) -> dict[str, Any] | None:
    """Return an eligible recoverable-operational task to OPEN with a fresh remediation budget. History, candidates and evidence stay."""
    info = classify_block(store, task_id)
    browser_upgrade = (
        _browser_evidence_upgrade(store, task_id, info) if info is not None else None
    )
    browser_expansion = (
        _browser_evidence_expansion(store, task_id, info) if info is not None else None
    )
    review_repair = (
        _review_lifecycle_repair(store, task_id, info) if info is not None else None
    )
    if reentry_refusal(store, task_id, fingerprint) is not None:
        return None
    assert info is not None
    task = store.get_task(task_id)
    stage = str(task["stage"]) if task is not None else None
    if review_repair is not None:
        if not store.unblock_task(task_id):
            return None
        if review_repair.get("synthetic_finding_id"):
            store.close_finding(str(review_repair["synthetic_finding_id"]))
        store.advance_task(task_id, Stage.REVIEW)
        entry = {
            "task_id": task_id,
            "auto_recovery": "review_role_lifecycle_repair",
            "attempt": 1,
            "of": 1,
            "stage": stage,
            "restored_stage": str(Stage.REVIEW),
            "was_blocked_because": info.reason[:300],
            "block_source": info.source,
            "runtime": fingerprint,
            **review_repair,
        }
        record_audit(store, REVIEW_LIFECYCLE_REPAIR_EVENT, entry)
        return entry
    if browser_upgrade is not None:
        if not store.unblock_task(task_id):
            return None
        entry = {
            "task_id": task_id,
            "auto_recovery": "verified_browser_evidence_upgrade",
            "budget_class": "verified_browser_evidence_upgrade",
            "attempt": 1,
            "of": 1,
            "stage": stage,
            "candidate_sha": info.candidate_sha,
            "capture_id": browser_upgrade["capture_id"],
            "was_blocked_because": info.reason[:300],
            "block_source": info.source,
            "runtime": fingerprint,
        }
        record_audit(store, BROWSER_EVIDENCE_UPGRADE_EVENT, entry)
        return entry
    if browser_expansion is not None:
        if not store.unblock_task(task_id):
            return None
        entry = {
            "task_id": task_id,
            "auto_recovery": "verified_browser_evidence_expansion",
            "budget_class": "verified_browser_evidence_expansion",
            "attempt": 1,
            "of": 1,
            "stage": stage,
            "candidate_sha": info.candidate_sha,
            "source_capture_id": browser_expansion.source_capture_id,
            "finding_digest": browser_expansion.finding_digest,
            "was_blocked_because": info.reason[:300],
            "block_source": info.source,
            "runtime": fingerprint,
        }
        record_audit(store, BROWSER_EVIDENCE_EXPANSION_REENTRY_EVENT, entry)
        return entry
    attempt = reentries_used(store, task_id) + 1  # counted before the unblock, which looks like an operator reset until our event follows
    if not store.unblock_task(task_id):
        return None
    entry = {
        "task_id": task_id,
        "auto_recovery": "blocked_reentry",
        "attempt": attempt,
        "of": MAX_REENTRIES,
        "stage": stage,
        "was_blocked_because": info.reason[:300],
        "block_source": info.source,
        "runtime": fingerprint,
    }
    record_audit(store, REENTER_EVENT, entry)
    return entry


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
