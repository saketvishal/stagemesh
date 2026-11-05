"""Execute only immutable pytest targets from an audited StageMesh CI plan."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stagemesh.ci_validation_plan import (
    CI_VALIDATION_CONTRACT,
    COMPONENT,
    audited_pytest_targets,
)

MAX_PLAN_BYTES = 1024 * 1024
SUPPORTED_OS = frozenset({"windows", "linux", "portable"})


def load_targets(path: Path, os_name: str) -> tuple[str, ...]:
    """Validate a bounded plan and return allowlisted targets for this OS."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("CI plan must be a regular non-symlink file")
    if path.stat().st_size > MAX_PLAN_BYTES:
        raise ValueError("CI plan exceeds the bounded size limit")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("CI plan root must be an object")
    if payload.get("contract") != CI_VALIDATION_CONTRACT:
        raise ValueError("CI plan contract is unsupported")
    if payload.get("mode") != COMPONENT:
        raise ValueError("CI component runner requires component mode")
    required_os = payload.get("required_os")
    if (
        not isinstance(required_os, list)
        or not required_os
        or any(item not in SUPPORTED_OS for item in required_os)
    ):
        raise ValueError("CI plan required_os is invalid")
    if os_name not in required_os and "portable" not in required_os:
        return ()
    direct = payload.get("direct_test_targets")
    dependent = payload.get("dependent_test_targets")
    if not isinstance(direct, list) or not direct or not isinstance(dependent, list):
        raise ValueError("CI component plan must contain direct and dependent target lists")
    targets = tuple(dict.fromkeys((*direct, *dependent)))
    allowed = audited_pytest_targets()
    if any(not isinstance(target, str) or target not in allowed for target in targets):
        raise ValueError("CI plan contains a target outside the immutable component allowlist")
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--os", choices=("windows", "linux"), required=True)
    args = parser.parse_args(argv)
    try:
        targets = load_targets(args.plan, args.os)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"component validation refused: {exc}", file=sys.stderr)
        return 2
    if not targets:
        print(f"component validation not required on {args.os}; no coverage claimed")
        return 0
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *targets],
        cwd=ROOT,
        check=False,
    )
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
