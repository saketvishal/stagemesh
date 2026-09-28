from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .completion_audit import completion_audit
from .persistence import Store


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    status: str
    detail: str


def run_command_check(name: str, command: list[str], root: Path) -> ReadinessCheck:
    result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
    status = "PASS" if result.returncode == 0 else "FAIL"
    detail = (result.stdout + result.stderr).strip()[-2000:]
    return ReadinessCheck(name, status, detail)


def release_readiness(
    root: Path, include_acceptance: bool = True, run_checks: bool = True, store: Store | None = None
) -> dict[str, object]:
    checks: list[ReadinessCheck] = []
    if run_checks:
        checks = [
            run_command_check("compile", [sys.executable, "-m", "compileall", "-q", "src", "scripts", "build_backend.py"], root),
            run_command_check("invariants", [sys.executable, "scripts/invariants.py"], root),
            run_command_check("provider_acceptance", [sys.executable, "scripts/provider_acceptance.py"], root),
            run_command_check("github_acceptance", [sys.executable, "scripts/github_acceptance.py"], root),
            run_command_check("live_acceptance", [sys.executable, "scripts/live_acceptance.py"], root),
            run_command_check(
                "install",
                [sys.executable, "-m", "pip", "install", ".", "--target", ".tmp-install", "--no-cache-dir", "--upgrade"],
                root,
            ),
        ]
        if include_acceptance:
            checks.insert(4, run_command_check("acceptance", [sys.executable, "scripts/acceptance.py"], root))
    audit = completion_audit(store)
    external_gaps = [
        item for item in audit["items"]
        if item["status"] in {"REQUIRES_CREDENTIALS", "MISSING_EXTERNAL_EVIDENCE", "INTERFACE_READY"}
    ]
    local_pass = all(check.status == "PASS" for check in checks) if checks else True
    return {
        "generated_at": time.time(),
        "local_status": "PASS" if local_pass else "FAIL",
        "overall_status": "BLOCKED_ON_EXTERNAL_EVIDENCE" if local_pass and external_gaps else ("PASS" if local_pass else "FAIL"),
        "checks": [check.__dict__ for check in checks],
        "external_gaps": external_gaps,
    }


def write_release_readiness(
    root: Path,
    output: Path,
    include_acceptance: bool = True,
    run_checks: bool = True,
    store: Store | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(release_readiness(root, include_acceptance, run_checks, store), indent=2, sort_keys=True),
        encoding="utf-8",
    )
