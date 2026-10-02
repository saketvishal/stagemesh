from __future__ import annotations

from pathlib import Path

from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .git import GitError, GitWorkspace
from .persistence import Store


class Integrator:
    """Integrate a validated candidate into the target checkout.

    apply=False preserves the lightweight deterministic test harness. Production
    CLI runs set apply=True so DONE requires a real git integration operation.
    """

    def __init__(self, apply: bool = False):
        self.apply = apply

    def integrate(
        self,
        store: Store,
        task_id: str,
        candidate_sha: str,
        project: Path,
    ) -> EvidenceStatus:
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.INTEGRATION,
            candidate_sha=candidate_sha,
        )

        if not self.apply:
            return self._finish(
                store,
                execution_id,
                task_id,
                candidate_sha,
                EvidenceStatus.PASSED,
                {"integrator": "builtin-noop"},
            )

        workspace = GitWorkspace(project)
        try:
            workspace.init_if_needed()
            exists = workspace.run(
                "cat-file",
                "-e",
                f"{candidate_sha}^{{commit}}",
                check=False,
            )
            if exists.returncode != 0:
                return self._finish(
                    store,
                    execution_id,
                    task_id,
                    candidate_sha,
                    EvidenceStatus.FAILED,
                    {
                        "integrator": "git",
                        "reason": "candidate commit does not exist",
                    },
                )

            dirty = workspace.run(
                "status",
                "--porcelain",
                check=False,
            ).stdout.strip()
            if dirty:
                return self._finish(
                    store,
                    execution_id,
                    task_id,
                    candidate_sha,
                    EvidenceStatus.FAILED,
                    {
                        "integrator": "git",
                        "reason": "target checkout is dirty",
                    },
                )

            head = workspace.head()
            if head == candidate_sha:
                return self._finish(
                    store,
                    execution_id,
                    task_id,
                    candidate_sha,
                    EvidenceStatus.PASSED,
                    {
                        "integrator": "git",
                        "mode": "already-integrated",
                        "integrated_head": head,
                    },
                )

            ancestor = workspace.run(
                "merge-base",
                "--is-ancestor",
                head,
                candidate_sha,
                check=False,
            )
            if ancestor.returncode == 0:
                result = workspace.run(
                    "merge",
                    "--ff-only",
                    candidate_sha,
                    check=False,
                )
                mode = "fast-forward"
            else:
                result = workspace.run(
                    "merge",
                    "--no-ff",
                    "--no-edit",
                    candidate_sha,
                    "-m",
                    f"StageMesh integrate {task_id}",
                    check=False,
                )
                mode = "merge"

            if result.returncode != 0:
                workspace.run("merge", "--abort", check=False)
                return self._finish(
                    store,
                    execution_id,
                    task_id,
                    candidate_sha,
                    EvidenceStatus.FAILED,
                    {
                        "integrator": "git",
                        "mode": mode,
                        "reason": (
                            result.stderr.strip()
                            or result.stdout.strip()
                            or "git integration failed"
                        )[-4000:],
                    },
                )

            integrated_head = workspace.head()
            return self._finish(
                store,
                execution_id,
                task_id,
                candidate_sha,
                EvidenceStatus.PASSED,
                {
                    "integrator": "git",
                    "mode": mode,
                    "integrated_head": integrated_head,
                },
            )
        except GitError as exc:
            return self._finish(
                store,
                execution_id,
                task_id,
                candidate_sha,
                EvidenceStatus.FAILED,
                {
                    "integrator": "git",
                    "reason": str(exc)[-4000:],
                },
            )

    @staticmethod
    def _finish(
        store: Store,
        execution_id: str,
        task_id: str,
        candidate_sha: str,
        status: EvidenceStatus,
        payload: dict[str, object],
    ) -> EvidenceStatus:
        store.add_evidence(
            task_id,
            candidate_sha,
            EvidenceKind.INTEGRATION,
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
