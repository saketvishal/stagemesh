"""`SupervisedIntegrator`: SerializedIntegrator whose stale/diverged-base handling is the supervisor's deterministic policy.

The base class rebases a diverged candidate in place. This subclass instead asks the supervisor, which distinguishes ordinary
advancement from rewritten history, preserves the original candidate, builds a provenance-carrying replacement, proves equivalence,
and sends the task back to VALIDATE so validation and independent review are rerun on the exact replacement.
"""

from __future__ import annotations

from pathlib import Path

from ..persistence import Store
from ..serialized_integration import REBASE_CONFLICT, STALE_BASE, SerializedIntegrator
from .decisions import Action
from .supervisor import Supervisor


class SupervisedIntegrator(SerializedIntegrator):
    def __init__(self, supervisor: Supervisor, *args: object, **kwargs: object):
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.supervisor = supervisor

    _content_landed = False

    def integrate(self, store: Store, task_id: str, candidate_sha: str, project: Path):  # type: ignore[no-untyped-def]
        self._content_landed = False
        return super().integrate(store, task_id, candidate_sha, project)

    def ref_contains(self, project: Path, candidate_sha: str) -> bool:
        """Landed means landed: the ref contains the candidate, or contains every change of it under another commit."""
        return super().ref_contains(project, candidate_sha) or self.supervisor.content_already_landed(candidate_sha)

    def _fast_forward(self, project, candidate_sha, payload, findings):  # type: ignore[no-untyped-def]
        if self._content_landed:  # nothing to move: the base already has this change
            tip = self._resolve(project)
            payload.update(integration_method="content_already_on_base", integration_ref_before=tip, integration_ref_after=tip)
            return
        super()._fast_forward(project, candidate_sha, payload, findings)

    def _rebase(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> bool:
        attempts = self._rebase_count(store, task_id)
        if attempts >= self.max_rebases:
            tip = self._resolve(project)
            self._typed_failure = (
                STALE_BASE,
                f"{self.integration_ref} advanced to {tip} again after {attempts} automatic refresh(es) of task {task_id}; ref left unchanged.",
            )
            self._emit(task_id, STALE_BASE, {"ref_tip": tip, "rebases": attempts})
            return False
        decision = self.supervisor.reconcile_base(task_id)
        if decision is not None and decision.detail.get("content_already_on_base"):
            self._content_landed = True  # integrate records the landing as it is instead of trying to move the ref
            return False
        if decision is None or decision.action is Action.PROCEED:
            return False  # nothing to refresh: the base integration check below decides
        if decision.action in {Action.REFRESH_CANDIDATE, Action.CREATE_RETARGETED_CANDIDATE}:
            self._emit(task_id, "integration_rebased", {k: v for k, v in decision.shas.items()})
            return True
        if decision.action is Action.RECONSTRUCT_ON_NEW_BASE:
            self._emit(task_id, "integration_reconstruct", {"original_candidate": candidate_sha})
            return True  # the supervisor already sent the task back to IMPLEMENT on the new base
        reason = decision.escalation.reason.value if decision.escalation else decision.condition.value
        self._typed_failure = (REBASE_CONFLICT, f"{decision.trace_line()} [escalation: {reason}]")
        self._emit(task_id, REBASE_CONFLICT, {"decision": decision.trace_line()})
        return False
