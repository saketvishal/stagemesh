from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .persistence import Store


class ValidationDiscoveryError(ValueError):
    pass


DEFAULT_SOURCE_TEST_MAPPING: dict[str, list[str]] = {
    "src/stagemesh/coordinator.py": ["tests/test_canary_regression.py", "tests/test_run_from_anywhere_and_multi_project.py"],
    "src/stagemesh/process_identity.py": ["tests/test_process_identity_verification.py"],
    "src/stagemesh/process_tree.py": ["tests/test_process_tree_termination.py"],
    "src/stagemesh/objectives.py": ["tests/test_planner_contract_validation.py"],
    "src/stagemesh/persistence.py": ["tests/test_sqlite_busy_retry.py"],
    "src/stagemesh/validation.py": ["tests/test_affected_test_discovery.py"],
    "src/stagemesh/observability.py": ["tests/test_metrics_observability.py"],
    "src/stagemesh/worktree.py": ["tests/test_worktree_provisioning.py", "tests/test_worktree_cleanup.py"],
    "src/stagemesh/providers.py": ["tests/test_provider_failover.py", "tests/test_reviewer_independence.py"],
    "src/stagemesh/labels.py": ["tests/test_github_lifecycle_labels.py"],
    "src/stagemesh/azure_devops.py": ["tests/test_azure_devops_task_source.py"],
    "src/stagemesh/watcher.py": ["tests/test_watcher_daemon_locking.py", "tests/test_watcher_cli.py"],
}


def _match_path(pattern: str, file_path: str) -> bool:
    """Match pattern against file_path anchored at path component boundaries."""
    p_parts = [p for p in pattern.replace("\\", "/").strip("/").split("/") if p]
    f_parts = [p for p in file_path.replace("\\", "/").strip("/").split("/") if p]
    if not p_parts or not f_parts or len(p_parts) > len(f_parts):
        return False
    return f_parts[-len(p_parts):] == p_parts


def git_changed_files(repo_path: Path, base_ref: str | None = None, head_ref: str | None = None) -> list[str]:
    """Return list of changed files in git repository."""
    import subprocess

    try:
        cmd = ["git", "diff", "--name-only"]
        if base_ref and head_ref:
            cmd.extend([base_ref, head_ref])
        elif base_ref:
            cmd.append(base_ref)
        else:
            cmd.append("HEAD")
        res = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            return [line.strip().replace("\\", "/") for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return []


class AffectedTestDiscovery:
    """Maps changed source files to focused test commands."""

    def __init__(self, mapping: dict[str, list[str]] | None = None, use_defaults: bool = False):
        if mapping is not None:
            self._mapping = dict(mapping)
        elif use_defaults:
            self._mapping = dict(DEFAULT_SOURCE_TEST_MAPPING)
        else:
            self._mapping = {}

    def _normalise(self, path: str | Path) -> str:
        return str(Path(path)).replace("\\", "/").strip()

    def discover(
        self,
        changed_files: Sequence[str | Path],
        *,
        baseline_commands: list[str] | None = None,
    ) -> list[str]:
        if not changed_files:
            return sorted(baseline_commands or [])

        commands: set[str] = set()
        has_unknown = False
        normed_changed = [self._normalise(f) for f in changed_files]

        for normed in normed_changed:
            matched = False
            for pattern, cmds in self._mapping.items():
                if _match_path(pattern, normed):
                    commands.update(cmds)
                    matched = True
            if not matched:
                has_unknown = True

        if has_unknown or not commands:
            commands.update(baseline_commands or [])

        return sorted(commands)

    def register(self, source_pattern: str, test_commands: list[str]) -> None:
        if not isinstance(source_pattern, str) or not source_pattern.strip():
            raise ValidationDiscoveryError("source pattern must be a non-empty string")
        if not isinstance(test_commands, list) or not test_commands:
            raise ValidationDiscoveryError("test_commands must be a non-empty list")
        self._mapping[source_pattern.strip()] = [str(c) for c in test_commands]


class Validator:
    def __init__(
        self,
        discovery: AffectedTestDiscovery | None = None,
        command_runner: Any = None,
    ) -> None:
        self.discovery = discovery or AffectedTestDiscovery(use_defaults=True)
        self.command_runner = command_runner

    def validate(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> EvidenceStatus:
        import subprocess

        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.VALIDATION,
            candidate_sha=candidate_sha,
        )

        has_tests = (project / "tests").is_dir()
        if not has_tests and self.command_runner is None:
            # Synthetic or test-less workspace; validate candidate existence
            status = EvidenceStatus.PASSED if candidate_sha else EvidenceStatus.FAILED
            store.add_evidence(task_id, candidate_sha, EvidenceKind.VALIDATION, status, {"validator": "builtin"})
            store.finish_execution(
                execution_id,
                ExecutionStatus.SUCCEEDED if status is EvidenceStatus.PASSED else ExecutionStatus.FAILED,
                candidate_sha,
            )
            return status

        changed = git_changed_files(project, f"{candidate_sha}~1", candidate_sha) if candidate_sha else []
        test_commands = self.discovery.discover(changed, baseline_commands=["pytest tests/ -q"])

        passed = bool(candidate_sha)
        results = []
        for cmd in test_commands:
            if not passed:
                break
            try:
                if self.command_runner is not None:
                    ok, _ = self.command_runner(cmd, cwd=project)
                    returncode = 0 if ok else 1
                else:
                    res = subprocess.run(cmd.split(), cwd=project, capture_output=True, text=True, check=False)
                    returncode = res.returncode
                results.append({"command": cmd, "exit_code": returncode})
                if returncode != 0:
                    passed = False
            except Exception as exc:
                results.append({"command": cmd, "error": str(exc), "exit_code": 1})
                passed = False

        status = EvidenceStatus.PASSED if passed else EvidenceStatus.FAILED
        store.add_evidence(task_id, candidate_sha, EvidenceKind.VALIDATION, status, {"results": results})
        store.finish_execution(
            execution_id,
            ExecutionStatus.SUCCEEDED if status is EvidenceStatus.PASSED else ExecutionStatus.FAILED,
            candidate_sha,
        )
        return status

