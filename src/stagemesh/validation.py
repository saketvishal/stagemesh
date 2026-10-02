from __future__ import annotations

from pathlib import Path

from .change_control import (
    ChangeContract,
    ChangeControlError,
    contract_definition_violations,
    contract_violations,
    diff_summary,
    git_diff_check,
    run_validation_commands,
)
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .persistence import Store
from .remediation import finding_identity


class Validator:
    def __init__(self, require_contract: bool = False):
        self.require_contract = require_contract

    def validate(
        self,
        store: Store,
        task_id: str,
        candidate_sha: str,
        project: Path,
    ) -> EvidenceStatus:
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.VALIDATION,
            candidate_sha=candidate_sha,
        )
        violations: list[str] = []
        command_payload: list[dict[str, object]] = []
        summary_payload: dict[str, object] = {}
        contract = None
        try:
            contract = ChangeContract.load(project, task_id)
            if contract is None and self.require_contract:
                violations.append("missing mandatory change contract")
            if contract is not None and self.require_contract:
                violations.extend(contract_definition_violations(contract))
            if contract is not None:
                summary = diff_summary(project, candidate_sha)
                summary_payload = {
                    "changed_files": list(summary.changed_files),
                    "changed_lines": summary.changed_lines,
                }
                violations.extend(contract_violations(contract, summary))
                diff_check = git_diff_check(project, candidate_sha)
                command_payload.append(
                    {
                        "command": diff_check.command,
                        "returncode": diff_check.returncode,
                        "stdout": diff_check.stdout[-4000:],
                        "stderr": diff_check.stderr[-4000:],
                    }
                )
                if diff_check.returncode != 0:
                    violations.append("git diff check failed")
                for result in run_validation_commands(
                    project,
                    candidate_sha,
                    contract.validation_commands,
                ):
                    command_payload.append(
                        {
                            "command": result.command,
                            "returncode": result.returncode,
                            "stdout": result.stdout[-4000:],
                            "stderr": result.stderr[-4000:],
                        }
                    )
                    if result.returncode != 0:
                        violations.append(
                            f"validation command failed ({result.returncode}): {result.command}"
                        )
                        break
            elif not candidate_sha:
                violations.append("candidate SHA is required")
        except ChangeControlError as exc:
            violations.append(str(exc))

        status = EvidenceStatus.FAILED if violations else EvidenceStatus.PASSED
        payload = {
            "validator": "builtin-change-control",
            "contract_required": self.require_contract,
            "contract_present": contract is not None,
            **summary_payload,
            "commands": command_payload,
            "violations": violations,
        }
        if violations:
            for message in violations:
                identity = finding_identity(candidate_sha, message)
                store.upsert_finding(
                    identity,
                    task_id,
                    candidate_sha,
                    "ERROR",
                    message,
                )
        store.add_evidence(
            task_id,
            candidate_sha,
            EvidenceKind.VALIDATION,
            status,
            payload,
        )
        store.finish_execution(
            execution_id,
            ExecutionStatus.SUCCEEDED
            if status is EvidenceStatus.PASSED
            else ExecutionStatus.FAILED,
            candidate_sha,
        )
        return status
