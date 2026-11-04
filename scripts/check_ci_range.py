"""Run mandatory static-diff and attribution checks for an exact CI candidate range."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
DIFF_EVENTS = frozenset({"pull_request", "merge_group", "push"})


def main() -> int:
    event = os.environ.get("STAGEMESH_CI_EVENT", "").strip().casefold()
    base = os.environ.get("STAGEMESH_CI_BASE_SHA", "").strip()
    head = os.environ.get("STAGEMESH_CI_HEAD_SHA", "").strip()
    if event not in DIFF_EVENTS:
        print("No candidate range for this full-regression event; range policy is not applicable.")
        return 0
    if not SHA.fullmatch(base) or not SHA.fullmatch(head) or set(base) == {"0"}:
        print("Candidate range identity is missing or invalid.", file=sys.stderr)
        return 2
    try:
        for revision in (base, head):
            subprocess.run(
                ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
                cwd=ROOT,
                check=True,
            )
        subprocess.run(
            [
                "git",
                "-c",
                "core.whitespace=cr-at-eol",
                "diff",
                "--check",
                f"{base}...{head}",
                "--",
            ],
            cwd=ROOT,
            check=True,
        )
        # Pull-request and merge-queue commits must also pass the candidate range
        # attribution policy.  A push to main can contain GitHub's merge commit;
        # its incoming PR commits were already checked by the trusted protected
        # attribution workflow, so this job applies only static diff policy there.
        if event != "push":
            result = subprocess.run(
                [sys.executable, "scripts/check_attribution.py", "--base", base, "--head", head],
                cwd=ROOT,
                check=False,
            )
            if result.returncode:
                return result.returncode
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Candidate range policy could not complete: {exc}", file=sys.stderr)
        return 2
    print("Static candidate diff policy passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
