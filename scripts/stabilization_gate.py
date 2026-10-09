"""Default StageMesh stabilization gate: the synthetic runtime failure matrix, in seconds, with no real providers.

Runs `stagemesh continue` against a fake provider CLI for every runtime failure mode (success, no change, timeout, quota, workspace
mutation, validation and review failure, fallback, exhaustion, stale remediation) and asserts it recovers without operator steps.
Every scenario owns its own temp project, so scenarios run in parallel, one pytest process each (no plugins required).

    python scripts/stabilization_gate.py            # whole gate
    python scripts/stabilization_gate.py -k quota   # scenarios whose name matches
    python scripts/stabilization_gate.py --jobs 1   # serial
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = "tests/test_stabilization_matrix.py"
# The one pinned regression the platform CI jobs have always run, kept as part of the gate.
PINNED = ("tests/test_provider_pool.py::test_verbose_workspace_mutation_is_quarantined_without_provider_cooldown",)


def pytest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )


def scenarios(keyword: str | None) -> list[str]:
    collected = pytest("--collect-only", "-q", MATRIX, *(["-k", keyword] if keyword else []))
    if collected.returncode not in (0, 5):
        raise SystemExit(collected.stdout + collected.stderr)
    return [line.strip() for line in collected.stdout.splitlines() if "::" in line]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-k", dest="keyword", help="only scenarios whose name matches this pytest -k expression")
    parser.add_argument("--jobs", type=int, default=min(16, os.cpu_count() or 2))
    args = parser.parse_args(argv)
    targets = scenarios(args.keyword) + ([] if args.keyword else list(PINNED))
    if not targets:
        print("stabilization gate: no scenarios selected", file=sys.stderr)
        return 2
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        results = list(zip(targets, pool.map(lambda target: pytest("-q", target), targets)))
    failed = [(target, run) for target, run in results if run.returncode != 0]
    for target, run in results:
        print(f"{'FAIL' if run.returncode else 'ok  '} {target.split('::', 1)[-1]}")
    for target, run in failed:
        print(f"\n===== {target} =====\n{run.stdout}{run.stderr}")
    verdict = f"{len(failed)} of {len(targets)} failed" if failed else f"all {len(targets)} passed"
    print(f"\nstabilization gate: {verdict} in {time.monotonic() - started:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
