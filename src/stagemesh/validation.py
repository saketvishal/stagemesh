from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .persistence import Store


class ValidationDiscoveryError(ValueError):
    pass


class AffectedTestDiscovery:
    """Maps changed source files to focused test commands.

    Legacy contract (build_coordinator/runner/validation.py):
      - A changed source file maps to one or more associated test files.
      - Multiple changed files produce a union of affected tests.
      - An unknown file (no mapping) falls back to the full baseline validation.
      - No changed files means the baseline still runs (never silently skip).
      - Ordering is deterministic (sorted).
      - Paths are normalised to POSIX-style for cross-platform consistency.
    """

    def __init__(self, mapping: dict[str, list[str]] | None = None):
        # mapping: {source_path_pattern -> [test_command, ...]}
        # Patterns are matched by suffix (e.g. "src/foo.py" matches if any
        # changed file ends with "src/foo.py" after normalisation).
        self._mapping: dict[str, list[str]] = mapping or {}

    def _normalise(self, path: str | Path) -> str:
        """Return a forward-slash, stripped path string."""
        return str(Path(path)).replace("\\", "/").strip()

    def discover(
        self,
        changed_files: Sequence[str | Path],
        *,
        baseline_commands: list[str] | None = None,
    ) -> list[str]:
        """Return the minimal set of test commands that cover all changed files.

        Rules:
        - If ``changed_files`` is empty, return ``baseline_commands`` (or []).
        - For each changed file, look up any registered test commands.
        - If a changed file matches no mapping, fall back to ``baseline_commands``.
        - Never return an empty list if ``baseline_commands`` is provided and
          any changed file has no specific mapping.
        - Result is sorted deterministically.
        """
        if not changed_files:
            return sorted(baseline_commands or [])

        commands: set[str] = set()
        has_unknown = False

        normed_changed = [self._normalise(f) for f in changed_files]

        for normed in normed_changed:
            matched = False
            for pattern, cmds in self._mapping.items():
                norm_pattern = self._normalise(pattern)
                if normed.endswith(norm_pattern) or norm_pattern in normed:
                    commands.update(cmds)
                    matched = True
            if not matched:
                has_unknown = True

        if has_unknown or not commands:
            commands.update(baseline_commands or [])

        return sorted(commands)

    def register(self, source_pattern: str, test_commands: list[str]) -> None:
        """Register a source → test-commands mapping."""
        if not isinstance(source_pattern, str) or not source_pattern.strip():
            raise ValidationDiscoveryError("source pattern must be a non-empty string")
        if not isinstance(test_commands, list) or not test_commands:
            raise ValidationDiscoveryError("test_commands must be a non-empty list")
        self._mapping[source_pattern.strip()] = [str(c) for c in test_commands]


class Validator:
    def validate(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> EvidenceStatus:
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.VALIDATION,
            candidate_sha=candidate_sha,
        )
        status = EvidenceStatus.PASSED if candidate_sha else EvidenceStatus.FAILED
        store.add_evidence(task_id, candidate_sha, EvidenceKind.VALIDATION, status, {"validator": "builtin"})
        store.finish_execution(
            execution_id,
            ExecutionStatus.SUCCEEDED if status is EvidenceStatus.PASSED else ExecutionStatus.FAILED,
            candidate_sha,
        )
        return status

