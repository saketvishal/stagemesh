"""Deterministic scope policy around an LLM review adapter.

The reviewer is an LLM and may flag anything. `ScopedReviewAdapter` post-processes its JSON verdict with `review_policy`: only
blocking findings inside the task's scope can fail the candidate (and be remediated); non-blocking observations and unrelated
suggestions are recorded as deferred work and do not change the candidate's scope. An unmet acceptance criterion that needs an
out-of-scope file blocks the task and escalates with a specific question instead of looping.

The wrapper is transparent to `Reviewer`: it exposes the inner adapter's `name` (also after a pool fallback) and the same call shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..contract_binding import contract_for_candidate
from ..persistence import Store
from .decisions import Action
from .review_policy import ReviewFindingInput, ReviewReport
from .scope import TaskScope, deferred_items, record_deferred

_NON_BLOCKING_SEVERITIES = frozenset({"minor", "nit", "nitpick", "info", "suggestion", "style", "low", "warning", "note", "trivial"})


def findings_from_response(raw: Any) -> list[ReviewFindingInput]:
    out: list[ReviewFindingInput] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            severity = str(item.get("severity") or "error")
            explicit = item.get("blocking")
            out.append(
                ReviewFindingInput(
                    message=str(item.get("message") or "independent review failed"),
                    severity=severity,
                    path=str(item["path"]) if item.get("path") else None,
                    blocking=explicit if isinstance(explicit, bool) else (False if severity.casefold() in _NON_BLOCKING_SEVERITIES else None),
                    acceptance_criterion=str(item["acceptance_criterion"]) if item.get("acceptance_criterion") else None,
                    category=str(item["category"]) if item.get("category") else None,
                )
            )
        else:
            out.append(ReviewFindingInput(message=str(item)))
    return out


class ScopedReviewAdapter:
    def __init__(self, inner: Any, supervisor: Any):
        self.inner = inner
        self.supervisor = supervisor

    @property
    def name(self) -> str:
        return str(getattr(self.inner, "name", "reviewer"))

    def review(self, prompt: str) -> str:
        return self.inner.review(prompt)

    def review_candidate(self, prompt: str, project: Path, candidate_sha: str) -> str:
        inner = getattr(self.inner, "review_candidate", None)
        response = inner(prompt, project, candidate_sha) if callable(inner) else self.inner.review(prompt)
        try:
            return self._scoped(response, candidate_sha)
        except Exception:  # noqa: BLE001 - policy must never turn a review problem into a pass; fall back to the raw verdict
            return response

    def _scoped(self, response: str, candidate_sha: str) -> str:
        try:
            parsed = json.loads(response)
        except (TypeError, ValueError):
            return response
        if not isinstance(parsed, dict) or parsed.get("decision") not in {"PASS", "FAIL"}:
            return response  # malformed / infrastructure verdicts are the Reviewer's to handle
        store: Store = self.supervisor.store
        task_id = _task_for_candidate(store, candidate_sha)
        if task_id is None:
            return response
        findings = findings_from_response(parsed.get("findings"))
        if not findings:
            return response
        scope = TaskScope.from_contract(
            contract_for_candidate(store, task_id, candidate_sha, self.supervisor.project).contract, deferred_items(store, task_id)
        )
        implementer = store.conn.execute("SELECT produced_by FROM candidates WHERE task_id=? AND sha=?", (task_id, candidate_sha)).fetchone()
        report = ReviewReport(candidate_sha, self.name, str(implementer["produced_by"]) if implementer else None, tuple(findings))
        assessment = self.supervisor.assess_review(task_id, report, scope, candidate_sha=candidate_sha, independent_required=False)
        if assessment.decision.action is Action.ESCALATE_TO_FOUNDER:
            store.block_task(task_id)  # remediation cannot fix a scope question: stop and ask the specific question
            blocking = [f for f in findings if f.is_blocking]
            return json.dumps({"decision": "FAIL", "findings": [{"severity": "error", "message": f.message} for f in blocking]})
        remaining = [*assessment.remediate, *assessment.fix_tests]
        for item in assessment.deferred:
            record_deferred(store, task_id, item)
        verdict: dict[str, Any] = {
            "decision": "FAIL" if remaining else "PASS",
            "deferred_findings": [item.to_dict() for item in assessment.deferred],
            "classified_by": "stagemesh-scope-policy",
        }
        if remaining:
            verdict["findings"] = [{"severity": f.severity, "message": f.message, **({"path": f.path} if f.path else {})} for f in remaining]
        return json.dumps(verdict)


class ScopedReviewPool:
    """Proxy for the provider pool's `review_adapter` so pooled (fallback-capable) reviewers are scoped too."""

    def __init__(self, pool: Any, supervisor: Any):
        self.pool = pool
        self.supervisor = supervisor

    def review_adapter(self, store: Store, task_id: str, candidate_sha: str, implementer: str | None):
        adapter, verdicts = self.pool.review_adapter(store, task_id, candidate_sha, implementer)
        return (ScopedReviewAdapter(adapter, self.supervisor) if adapter is not None else adapter), verdicts

    def __getattr__(self, item: str) -> Any:
        return getattr(self.pool, item)


def supervise_reviewer(reviewer: Any, supervisor: Any) -> Any:
    """Put the scope policy around whatever review adapter or pool the Reviewer uses."""
    if reviewer.review_pool is not None:
        reviewer.review_pool = ScopedReviewPool(reviewer.review_pool, supervisor)
    if reviewer.adapter is not None:
        reviewer.adapter = ScopedReviewAdapter(reviewer.adapter, supervisor)
    return reviewer


def _task_for_candidate(store: Store, candidate_sha: str) -> str | None:
    rows = store.conn.execute("SELECT DISTINCT task_id FROM candidates WHERE sha=?", (candidate_sha,)).fetchall()
    return str(rows[0]["task_id"]) if len(rows) == 1 else None  # ambiguous: leave the raw verdict alone (fail closed)


__all__ = ["ScopedReviewAdapter", "ScopedReviewPool", "findings_from_response", "supervise_reviewer"]
