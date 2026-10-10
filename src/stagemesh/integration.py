from __future__ import annotations

import json
from pathlib import Path

from .contract_binding import contract_for_candidate
from .contracts import ContractError, evaluate_contract
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .git import GitWorkspace
from .git_identity import attribution_offences
from .persistence import Store
from .remediation import finding_identity
from .review import independent_review_verified


class Integrator:
    """Verifies exact-SHA evidence, then fast-forwards `integration_ref` to the candidate.

    With no `integration_ref` it is evidence-only (synthetic/test use); the production CLI always sets one.
    """

    def __init__(self, integration_ref: str | None = None, require_independent_review: bool = False):
        self.integration_ref = integration_ref
        self.require_independent_review = require_independent_review

    def ref_contains(self, project: Path, candidate_sha: str) -> bool:
        if self.integration_ref is None:
            return False
        git = GitWorkspace(project)
        return git.run("merge-base", "--is-ancestor", candidate_sha, self.integration_ref, check=False).returncode == 0

    def integrate(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> EvidenceStatus:
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.INTEGRATION,
            candidate_sha=candidate_sha,
            actor="stagemesh-integrator",
        )
        findings: list[dict[str, object]] = []
        payload: dict[str, object] = {
            "integrator": "builtin",
            "candidate_sha": candidate_sha,
            "integration_ref": self.integration_ref,
            "independent_review_required": self.require_independent_review,
            "independent_review_verified": False,
        }
        try:
            bound = contract_for_candidate(store, task_id, candidate_sha, project)
            missing = [
                kind.value
                for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW)
                if not _has_exact_bound_evidence(store, task_id, candidate_sha, kind, bound)
            ]
            if not missing:
                payload["independent_review_verified"] = _has_exact_bound_evidence(
                    store, task_id, candidate_sha, EvidenceKind.REVIEW, bound, require_independent=True
                )
                if self.require_independent_review and not payload["independent_review_verified"]:
                    missing.append("independent REVIEW")
            if missing:
                findings.append(
                    {
                        "severity": "error",
                        "code": "missing_required_bound_evidence",
                        "message": "candidate is missing required passing bound evidence: " + ", ".join(missing),
                    }
                )
            contract = bound.contract
            evaluation = evaluate_contract(
                project,
                candidate_sha,
                contract,
                baseline_sha=bound.baseline_sha,
                run_gates=False,
            )
            payload.update(bound.evidence_payload())
            findings.extend(evaluation.findings)
            findings.extend(_attribution_findings(project, bound.baseline_sha, candidate_sha))
        except ContractError as exc:
            findings.append({"severity": "error", "code": "invalid_contract", "message": str(exc)})

        if not findings and self.integration_ref is not None:
            self._fast_forward(project, candidate_sha, payload, findings)

        status = EvidenceStatus.PASSED if not findings else EvidenceStatus.FAILED
        store.add_evidence(
            task_id,
            candidate_sha,
            EvidenceKind.INTEGRATION,
            status,
            {**payload, "findings": findings},
        )
        if status is EvidenceStatus.FAILED:
            for item in findings:
                message = str(item.get("message", "integration failed"))
                store.upsert_finding(
                    finding_identity(candidate_sha, message, item.get("path")),
                    task_id,
                    candidate_sha,
                    str(item.get("severity", "error")),
                    message,
                )
        store.finish_execution(
            execution_id,
            ExecutionStatus.SUCCEEDED if status is EvidenceStatus.PASSED else ExecutionStatus.FAILED,
            candidate_sha,
        )
        return status

    def _fast_forward(
        self, project: Path, candidate_sha: str, payload: dict[str, object], findings: list[dict[str, object]]
    ) -> None:
        """Move the integration ref to exactly `candidate_sha` using fast-forward-only git semantics."""
        ref = str(self.integration_ref)
        git = GitWorkspace(project)

        def resolve() -> str | None:
            result = git.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
            return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None

        def fail(code: str, message: str) -> None:
            findings.append({"severity": "error", "code": code, "message": message})

        before = resolve()
        payload["integration_ref_before"] = before
        payload["integration_ref_after"] = before
        if before is None:
            fail("integration_ref_missing", f"integration ref {ref} does not exist")
            return
        if before == candidate_sha:
            payload["integration_method"] = "already_integrated"
        elif git.run("merge-base", "--is-ancestor", candidate_sha, before, check=False).returncode == 0:
            # A previous run moved the ref to the candidate and the ref has advanced since (restart, or other tasks landed).
            payload["integration_method"] = "already_integrated"
            return
        else:
            if git.run("merge-base", "--is-ancestor", before, candidate_sha, check=False).returncode != 0:
                fail(
                    "integration_non_fast_forward",
                    f"{ref} at {before} cannot be fast-forwarded to {candidate_sha}; ref left unchanged",
                )
                return
            checked_out = git.run("symbolic-ref", "-q", "HEAD", check=False).stdout.strip() == ref
            if checked_out:
                payload["integration_method"] = "merge_ff_only"
                result = git.run("merge", "--ff-only", candidate_sha, check=False)
            else:
                payload["integration_method"] = "update_ref_ff_only"
                result = git.run("update-ref", ref, candidate_sha, before, check=False)  # compare-and-swap
            if result.returncode != 0:
                fail("integration_ref_update_failed", (result.stderr or result.stdout).strip() or "git update failed")
        after = resolve()
        payload["integration_ref_after"] = after
        if after != candidate_sha:
            fail(
                "integration_ref_not_updated",
                f"{ref} resolves to {after}, not candidate {candidate_sha}",
            )


def _attribution_findings(project: Path, baseline_sha: str | None, candidate_sha: str) -> list[dict[str, object]]:
    """Refuse to land commits that credit an AI provider, a StageMesh worker or a placeholder GitHub account as contributor.

    Only the candidate's own commits (baseline..candidate) are inspected, so existing history never blocks new work.
    """
    if not baseline_sha:
        return []
    result = GitWorkspace(project).run(
        "log", "--format=%H%x1f%an <%ae>%x1f%cn <%ce>%x1f%B%x1e", f"{baseline_sha}..{candidate_sha}", check=False, encoding="utf-8"
    )
    if result.returncode != 0:
        return []
    commits = []
    for record in result.stdout.split("\x1e"):
        parts = record.strip("\n").split("\x1f", 3)
        if len(parts) == 4:
            commits.append((parts[0], parts[1], parts[2], parts[3]))
    offences = attribution_offences(commits)
    if not offences:
        return []
    return [
        {
            "severity": "error",
            "code": "attribution_violation",
            "message": "candidate commits credit a non-owner contributor: " + "; ".join(offences[:5])
            + ". Commit as the repository owner and drop tool Co-authored-by trailers (providers belong in StageMesh metadata).",
        }
    ]


def _has_exact_bound_evidence(
    store: Store,
    task_id: str,
    candidate_sha: str,
    kind: EvidenceKind,
    bound: object,
    require_independent: bool = False,
) -> bool:
    rows = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
        (task_id, candidate_sha, kind, EvidenceStatus.PASSED),
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            payload.get("contract_hash") == bound.digest
            and payload.get("contract_version") == bound.version
            and payload.get("baseline_sha") == bound.baseline_sha
            and payload.get("candidate_sha") == candidate_sha
            and (not require_independent or independent_review_verified(payload))
        ):
            return True
    return False
