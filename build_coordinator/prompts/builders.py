"""Role-specific prompt builders for external coding agents."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from build_coordinator.execution.results import result_file_contract_for_role
from build_coordinator.types import ResumeContext

GLOBAL_BUILD_POLICY = (
    "You are executing engineering infrastructure work. "
    "Architecture correctness is more important than task completion. "
    "Stay within the stored task envelope, preserve protected WIP, do not "
    "force push, do not rebase published feature history, stage explicitly, "
    "do not persist hidden reasoning, and escalate on scope, security, "
    "migration, merge, or architecture conflicts."
)


def _context_payload(context: ResumeContext) -> dict[str, Any]:
    payload = asdict(context)
    for key, value in list(payload.items()):
        if isinstance(value, tuple):
            payload[key] = list(value)
        elif hasattr(value, "isoformat"):
            payload[key] = value.isoformat()
    return payload


class _PromptBuilder:
    role = "BUILDER"
    role_policy = ""

    def build(
        self,
        context: ResumeContext,
        *,
        extra: dict[str, Any] | None = None,
        repo_root: Any | None = None,
    ) -> str:
        extra = extra or {}
        envelope = _context_payload(context)
        if repo_root is None:
            from build_coordinator.config import get_settings

            try:
                repo_root = get_settings().repo_root
            except Exception:
                repo_root = None
        if repo_root:
            from build_coordinator.task_context import assemble_task_context

            try:
                bounded = assemble_task_context(repo_root, context, extra=extra)
                envelope["bounded_module_context"] = bounded.as_dict()
            except Exception:
                pass
        payload = {
            "global_build_policy": GLOBAL_BUILD_POLICY,
            "role_policy": self.role_policy,
            "task_definition": extra.get("task_definition"),
            "task_envelope": envelope,
            "resume_context": extra,
            "result_file_contract": extra.get("result_file_contract")
            or result_file_contract_for_role(self.role),
        }
        return json.dumps(payload, indent=2, sort_keys=True)


class BuilderPromptBuilder(_PromptBuilder):
    role = "BUILDER"
    role_policy = (
        "Implement the task contract, run required validation, checkpoint "
        "safe facts, and write a structured builder result JSON file to the "
        "runner-supplied result path. Do not perform independent review or "
        "integration. Do not persist secrets or hidden reasoning. "
        "A task branch has been assigned for this work: all reviewable "
        "output MUST be captured as commits on that branch before you "
        "report success -- uncommitted working-tree changes are not "
        "reviewable and will be treated as no work having been done. "
        "Never check out, create, or commit to any branch other than the "
        "one assigned to this task."
    )


class ReviewerPromptBuilder(_PromptBuilder):
    role = "REVIEWER"
    role_policy = (
        "Perform independent review against the scoped task contract and "
        "write structured ReviewVerdict JSON to the runner-supplied result "
        "file. Do not modify product code. Do not treat free-form prose as "
        "the lifecycle result. reviewed_feature_sha must match the "
        "runner-captured SHA supplied in this prompt. Your worktree is "
        "StageMesh-managed and may not be on the feature branch: inspect the "
        "code at that SHA (for example `git checkout --detach <sha>` in your "
        "own worktree) and never commit. "
        "If verification cannot be completed because review infrastructure, "
        "environment, or required tooling is unavailable, emit verdict "
        "REVIEW_ENVIRONMENT_BLOCKED with diagnostic findings instead of requesting "
        "source-code remediation. Use REMEDIATION_REQUIRED only when the implementation "
        "itself violates requirements."
    )

    def build(
        self,
        context: ResumeContext,
        *,
        extra: dict[str, Any] | None = None,
        repo_root: Any | None = None,
    ) -> str:
        extra = extra or {}
        convergence = extra.get("convergence_review")
        original_policy = self.role_policy
        if isinstance(convergence, dict) and convergence.get("pending_comprehensive_review"):
            self.role_policy = (
                original_policy
                + " This is a comprehensive convergence review after serial finding churn. "
                "Return the complete current substantive finding set in one pass, explicitly "
                "reconcile every prior finding id in finding_dispositions, and do not drip-feed "
                "one new finding at a time."
            )
        try:
            return super().build(context, extra=extra, repo_root=repo_root)
        finally:
            self.role_policy = original_policy


class RemediationPromptBuilder(_PromptBuilder):
    role = "REMEDIATION"
    role_policy = (
        "Address reviewer findings with additive remediation commits only. "
        "Do not rewrite history. Return to independent review."
    )

    def build(
        self,
        context: ResumeContext,
        *,
        extra: dict[str, Any] | None = None,
        repo_root: Any | None = None,
    ) -> str:
        extra = extra or {}
        conflict_rec = extra.get("conflict_recovery")
        if conflict_rec:
            paths = conflict_rec.get("conflict_paths", [])
            original_sha = conflict_rec.get("original_reviewed_sha") or conflict_rec.get("task_sha")
            current_main_sha = (
                conflict_rec.get("conflicting_current_main_sha")
                or conflict_rec.get("current_main_sha")
            )
            self.role_policy = (
                f"Resolve the Git merge conflict in conflicting files ({', '.join(paths)}). "
                f"The prior reviewed feature SHA is {original_sha}; current main is {current_main_sha}. "
                "Preserve both the task implementation and the changes that landed on main. "
                "Work on the existing task branch lineage against freshly fetched authoritative main; "
                "do not create a duplicate task branch or rewrite published history. "
                "Stage resolved files and commit the conflict resolution so the feature SHA changes. "
                "The old review approval is not authoritative for the new SHA; after validation, "
                "StageMesh will require independent review of the exact conflict-resolved SHA."
            )
        return super().build(context, extra=extra, repo_root=repo_root)



class PlannerPromptBuilder(_PromptBuilder):
    role = "PLANNER"
    role_policy = (
        "Decompose the free-text objective into a structured ObjectivePlan. "
        "You may propose child tasks. You may request typed human gates. "
        "requested_human_gates MUST be an array of strings, each exactly one "
        "of: ARCHITECTURE_DECISION_REQUIRED, SECURITY_DECISION_REQUIRED, "
        "PRIVACY_DECISION_REQUIRED, EXTERNAL_COST_APPROVAL_REQUIRED, "
        "CREDENTIAL_REQUIRED, DESTRUCTIVE_ACTION_APPROVAL_REQUIRED, "
        "LEGAL_POLICY_DECISION_REQUIRED, MAJOR_SCOPE_EXPANSION_REQUIRED, "
        "REMOTE_MAIN_PUSH_APPROVAL_REQUIRED, UNRESOLVABLE_CONFLICT. "
        "Do not emit objects, free-text gate names, worktrees, worker ids, "
        "branches, auto_push, or review_policy NONE/SELF. "
        "Each task MUST include task_id, title, goal, description, scope, "
        "prohibited_scope, dependencies, parallel_safe, risk_level "
        "(LOW|MEDIUM|HIGH|CRITICAL), and reason_created (short code such as "
        "OBJECTIVE_PLAN). "
        "You must NOT choose worktrees, assign workers, authorize remote "
        "main push, weaken review policy, use credentials, or persist "
        "chain-of-thought. Write one FULL executor-result JSON object to the "
        "runner-supplied result path. The top-level object MUST include "
        "schema_version, execution_id, task_id, role, status, and plan. "
        "Use the runner-supplied identity environment values for execution_id, "
        "task_id, and role; role MUST be PLANNER and successful completion MUST "
        "use status SUCCEEDED. Put the ObjectivePlan under the top-level plan "
        "field. Do NOT write a bare ObjectivePlan object."
    )


class IntegrationPromptBuilder(_PromptBuilder):
    role = "INTEGRATION"
    role_policy = (
        "Prepare privileged integration only after structured review approval. "
        "Mechanical Git compatibility is enforced by runner infrastructure; "
        "do not discover merge conflicts by inspection alone. Contribute "
        "semantic architecture notes if needed, but do not be the sole "
        "source of the Q record path or reviewed SHA. Write structured "
        "integrator result JSON to the runner-supplied result file. Stop "
        "for push approval when policy requires it. Never force-push or "
        "rewrite published history."
    )
