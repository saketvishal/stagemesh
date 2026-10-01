from __future__ import annotations

import os
from pathlib import Path
import shlex
import sys
from typing import Any, Sequence

from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .persistence import Store


class ValidationDiscoveryError(ValueError):
    pass


DEFAULT_SOURCE_TEST_MAPPING: dict[str, list[str]] = {
    "src/stagemesh/coordinator.py": ["pytest tests/test_canary_regression.py -q", "pytest tests/test_run_from_anywhere_and_multi_project.py -q"],
    "src/stagemesh/process_identity.py": ["pytest tests/test_process_identity_verification.py -q"],
    "src/stagemesh/process_tree.py": ["pytest tests/test_process_tree_termination.py -q"],
    "src/stagemesh/objectives.py": ["pytest tests/test_planner_contract_validation.py -q"],
    "src/stagemesh/persistence.py": ["pytest tests/test_sqlite_busy_retry.py -q"],
    "src/stagemesh/validation.py": ["pytest tests/test_affected_test_discovery.py -q"],
    "src/stagemesh/controlled_change.py": ["pytest tests/test_controlled_change.py -q"],
    "src/stagemesh/governance.py": ["pytest tests/test_git_governance.py -q"],
    "src/stagemesh/observability.py": ["pytest tests/test_metrics_observability.py -q"],
    "src/stagemesh/worktree.py": ["pytest tests/test_worktree_provisioning.py -q", "pytest tests/test_worktree_cleanup.py -q"],
    "src/stagemesh/providers.py": ["pytest tests/test_provider_failover.py -q", "pytest tests/test_reviewer_independence.py -q"],
    "src/stagemesh/labels.py": ["pytest tests/test_github_lifecycle_labels.py -q"],
    "src/stagemesh/azure_devops.py": ["pytest tests/test_azure_devops_task_source.py -q"],
    "src/stagemesh/watcher.py": ["pytest tests/test_watcher_daemon_locking.py -q", "pytest tests/test_watcher_cli.py -q"],
}


def split_command(command: str) -> list[str]:
    """Split command string safely using legacy-equivalent platform quoting."""
    parts = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        parts = [part[1:-1] if len(part) > 1 and part[0] == part[-1] and part[0] in "\"'" else part for part in parts]
    if not parts:
        raise ValueError("empty validation command")
    return parts


def build_executable_argv(command: str | Sequence[str]) -> list[str]:
    """Construct an executable argv list, ensuring .py test files are never directly invoked."""
    if isinstance(command, str):
        argv = split_command(command)
    else:
        argv = list(command)
    if not argv:
        raise ValueError("empty validation command")

    if argv[0].endswith(".py") or Path(argv[0]).suffix == ".py":
        return [sys.executable, "-m", "pytest", *argv]
    if argv[0] == "pytest":
        return [sys.executable, "-m", "pytest", *argv[1:]]
    if argv[0] in ("python", "python3") and len(argv) > 1 and argv[1] == "-m":
        return [sys.executable, *argv[1:]]
    return argv


def _match_path(pattern: str, file_path: str) -> bool:
    """Match pattern against file_path anchored at path component boundaries."""
    p_parts = [p for p in pattern.replace("\\", "/").strip("/").split("/") if p]
    f_parts = [p for p in file_path.replace("\\", "/").strip("/").split("/") if p]
    if not p_parts or not f_parts or len(p_parts) > len(f_parts):
        return False
    return f_parts[-len(p_parts):] == p_parts


def git_changed_files(repo_path: Path, base_ref: str | None = None, head_ref: str | None = None) -> list[str]:
    """Return list of changed files in git repository across entire base_ref..head_ref range."""
    import subprocess

    try:
        cmd = ["git", "diff", "--name-only"]
        if base_ref and head_ref:
            cmd.extend([base_ref, head_ref])
        elif base_ref:
            cmd.append(base_ref)
        elif head_ref:
            cmd.append(head_ref)
        else:
            cmd.append("HEAD")
        res = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            return [line.strip().replace("\\", "/") for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return []


def resolve_baseline_sha(repo_path: Path, candidate_sha: str) -> str | None:
    """Determine baseline commit for candidate_sha across multi-commit ranges."""
    import subprocess

    for target in ("origin/main", "main", "origin/master", "master", "HEAD"):
        try:
            res = subprocess.run(
                ["git", "merge-base", target, candidate_sha],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                mb = res.stdout.strip()
                if mb and mb != candidate_sha:
                    return mb
        except Exception:
            pass
    try:
        res = subprocess.run(
            ["git", "rev-parse", f"{candidate_sha}~1"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


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

    def validate(
        self,
        store: Store,
        task_id: str,
        candidate_sha: str,
        project: Path,
        *,
        base_sha: str | None = None,
    ) -> EvidenceStatus:
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

        # Check if a ChangeContract exists for this task
        contract = store.get_change_contract(task_id)
        changeset = None
        plan = None
        platform_key = None
        if contract is not None and candidate_sha:
            from .controlled_change import (
                current_platform_key,
                derive_git_changeset,
                enforce_change_scope,
                ScopeViolationError,
                ValidationPlanner,
            )
            changeset = derive_git_changeset(project, task_id, contract.baseline_sha, candidate_sha)
            store.save_change_set(changeset)

            scope_result = enforce_change_scope(contract, changeset)
            if not scope_result.is_authorized:
                store.add_evidence(
                    task_id,
                    candidate_sha,
                    EvidenceKind.VALIDATION,
                    EvidenceStatus.FAILED,
                    {"scope_violation": scope_result.to_dict()},
                )
                store.finish_execution(
                    execution_id,
                    ExecutionStatus.FAILED,
                    candidate_sha,
                )
                raise ScopeViolationError(scope_result)

            planner = ValidationPlanner(self.discovery)
            plan = planner.plan(contract, changeset)
            store.save_validation_plan(plan, changeset.result_tree_sha)

            platform_key = current_platform_key()
            cached = store.get_validation_cache(changeset.result_tree_sha, plan.plan_hash, platform_key)
            if cached and cached["status"] == EvidenceStatus.PASSED.value:
                store.add_evidence(
                    task_id,
                    candidate_sha,
                    EvidenceKind.VALIDATION,
                    EvidenceStatus.PASSED,
                    {"reused": True, "plan_hash": plan.plan_hash, **cached["payload"]},
                )
                store.finish_execution(
                    execution_id,
                    ExecutionStatus.SUCCEEDED,
                    candidate_sha,
                )
                return EvidenceStatus.PASSED

            test_commands = list(plan.selected_commands)
        else:
            # Resolve full baseline range so multi-commit candidates do not hide changed files
            if base_sha is None:
                try:
                    row = store.conn.execute(
                        "SELECT base_sha FROM candidates WHERE task_id=? AND sha=?",
                        (task_id, candidate_sha),
                    ).fetchone()
                    if row and row["base_sha"]:
                        base_sha = str(row["base_sha"])
                except Exception:
                    pass
            effective_base = base_sha or resolve_baseline_sha(project, candidate_sha)

            if candidate_sha and effective_base:
                changed = git_changed_files(project, effective_base, candidate_sha)
            elif candidate_sha:
                changed = git_changed_files(project, head_ref=candidate_sha)
            else:
                changed = []

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
                    argv = build_executable_argv(cmd)
                    res = subprocess.run(argv, cwd=project, capture_output=True, text=True, check=False)
                    returncode = res.returncode
                results.append({"command": cmd, "exit_code": returncode})
                if returncode != 0:
                    passed = False
            except Exception as exc:
                results.append({"command": cmd, "error": str(exc), "exit_code": 1})
                passed = False

        status = EvidenceStatus.PASSED if passed else EvidenceStatus.FAILED
        payload = {"results": results}
        if plan is not None:
            payload["plan_hash"] = plan.plan_hash
            payload["validation_level"] = plan.validation_level
            if passed and changeset is not None and platform_key is not None:
                store.record_validation_cache(
                    changeset.result_tree_sha,
                    plan.plan_hash,
                    platform_key,
                    status.value,
                    {"results": results},
                )

        store.add_evidence(task_id, candidate_sha, EvidenceKind.VALIDATION, status, payload)
        store.finish_execution(
            execution_id,
            ExecutionStatus.SUCCEEDED if status is EvidenceStatus.PASSED else ExecutionStatus.FAILED,
            candidate_sha,
        )
        return status

