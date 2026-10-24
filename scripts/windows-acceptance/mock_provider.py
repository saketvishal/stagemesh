"""Deterministic mock coding provider for the disposable driver acceptance project.

It never contacts a network or a model. Implementation writes a fixed file; review always approves. Every call is appended
to the JSONL file named by --calls so the harness can prove how many times the scheduler really reached a provider.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", required=True)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--fail", action="store_true")
    args, _unknown = parser.parse_known_args()
    prompt = sys.stdin.read()
    review = '"decision"' in prompt or "PASS" in prompt and "FAIL_WITH_FINDINGS" in prompt
    stage = "review" if review else "implement"
    with open(args.calls, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"t": time.time(), "pid": os.getpid(), "stage": stage, "cwd": os.getcwd()}) + "\n")
    if args.sleep:
        time.sleep(args.sleep)
    if args.fail:
        print("mock provider forced failure", file=sys.stderr)
        return 1
    if review:
        print(json.dumps({"decision": "PASS", "findings": []}))
        return 0
    # Unique per call: identical content for every task would be "no change" after the first task is integrated.
    Path("mock-output.txt").write_text(f"mock provider output {os.getpid()} {time.time()}\n", encoding="utf-8")
    print("wrote mock-output.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
