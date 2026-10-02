from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .acceptance import AcceptanceCheck, proof_gaps
from .completion_audit import completion_audit
from .external_evidence import external_evidence_records
from .persistence import Store
from .security import WorkspaceBoundary


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    status: str
    detail: str


class ReleaseReadinessValidationError(ValueError):
    pass


def run_command_check(name: str, command: list[str], root: Path) -> ReadinessCheck:
    name = _validate_text(name, "readiness check name")
    command = _validate_command(command)
    root = _validate_root(root)
    result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
    status = "PASS" if result.returncode == 0 else "FAIL"
    detail = (result.stdout + result.stderr).strip()[-2000:]
    return ReadinessCheck(name, status, detail)


def release_readiness(
    root: Path,
    include_acceptance: bool = True,
    run_checks: bool = True,
    store: Store | None = None,
    candidate_sha: str | None = None,
) -> dict[str, object]:
    root = _validate_root(root)
    checks: list[ReadinessCheck] = []
    if run_checks:
        checks = [
            run_command_check("compile", [sys.executable, "-m", "compileall", "-q", "src", "scripts", "build_backend.py"], root),
            run_command_check("invariants", [sys.executable, "scripts/invariants.py"], root),
            run_command_check("provider_acceptance", [sys.executable, "scripts/provider_acceptance.py"], root),
            run_command_check("github_acceptance", [sys.executable, "scripts/github_acceptance.py"], root),
            run_command_check("live_acceptance", [sys.executable, "scripts/live_acceptance.py", "--json"], root),
            run_command_check(
                "change_control_acceptance",
                [sys.executable, "scripts/change_control_acceptance.py"],
                root,
            ),
            run_command_check(
                "install",
                [sys.executable, "-m", "pip", "install", ".", "--target", ".tmp-install", "--no-cache-dir", "--upgrade"],
                root,
            ),
        ]
        if include_acceptance:
            checks.insert(4, run_command_check("acceptance", [sys.executable, "scripts/acceptance.py"], root))
    audit = completion_audit(store, candidate_sha)
    external_gaps = [
        item for item in audit["items"]
        if item["status"] in {"REQUIRES_CREDENTIALS", "MISSING_EXTERNAL_EVIDENCE", "INTERFACE_READY"}
    ]
    evidence = _evidence_summary(store, candidate_sha)
    local_pass = all(check.status == "PASS" for check in checks) if checks else True
    local_gaps = proof_gaps([AcceptanceCheck(check.name, check.status, check.detail) for check in checks])
    return {
        "generated_at": time.time(),
        "local_status": "PASS" if local_pass else "FAIL",
        "overall_status": "BLOCKED_ON_EXTERNAL_EVIDENCE" if local_pass and external_gaps else ("PASS" if local_pass else "FAIL"),
        "checks": [check.__dict__ for check in checks],
        "local_proof_gaps": local_gaps,
        "external_evidence": evidence,
        "external_gaps": external_gaps,
    }


def write_release_readiness(
    root: Path,
    output: Path,
    include_acceptance: bool = True,
    run_checks: bool = True,
    store: Store | None = None,
    candidate_sha: str | None = None,
) -> None:
    root = _validate_root(root)
    output = WorkspaceBoundary(root).require_inside(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(release_readiness(root, include_acceptance, run_checks, store, candidate_sha), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _validate_text(value: str, field: str, max_length: int = 200) -> str:
    if not isinstance(value, str):
        raise ReleaseReadinessValidationError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ReleaseReadinessValidationError(f"{field} must be a non-empty string")
    if len(normalized) > max_length:
        raise ReleaseReadinessValidationError(f"{field} must be {max_length} characters or fewer")
    return normalized


def _validate_command(command: list[str]) -> list[str]:
    if not isinstance(command, list) or not command:
        raise ReleaseReadinessValidationError("readiness command must be a non-empty list")
    return [_validate_text(arg, "readiness command argument", 1000) for arg in command]


def _validate_root(root: Path) -> Path:
    resolved = Path(root).resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ReleaseReadinessValidationError("readiness root must be an existing directory")
    return resolved


def _evidence_summary(store: Store | None, candidate_sha: str | None) -> list[dict[str, object]]:
    if store is None:
        return []
    return [
        {
            "kind": record.kind,
            "status": record.status,
            "url": record.url,
            "candidate_sha": record.candidate_sha,
            "candidate_match": candidate_sha is not None and record.candidate_sha == candidate_sha,
            "notes": record.notes,
        }
        for record in external_evidence_records(store)
    ]
