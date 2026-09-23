"""Deterministic execution of a task's `validation:` commands.

The commands come from the project's version-controlled task definition (via
synchronization), never from executor output. StageMesh runs them itself in the
task workspace and the result, not an agent's claim, gates the lifecycle.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OUTPUT_TAIL_CHARS = 2000


@dataclass
class ValidationOutcome:
    passed: bool
    results: list[dict[str, Any]] = field(default_factory=list)

    def failure_summary(self) -> list[str]:
        return [
            f"{item['command']} -> exit {item['exit_code']}: {item['output_tail'][-500:]}"
            for item in self.results
            if item["exit_code"] != 0
        ]


def split_command(command: str) -> list[str]:
    parts = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        parts = [part[1:-1] if len(part) > 1 and part[0] == part[-1] and part[0] in "\"'" else part for part in parts]
    if not parts:
        raise ValueError("empty validation command")
    return parts


def run_validation(
    commands: list[str],
    cwd: str | Path,
    *,
    timeout_seconds: float = 900,
    env: dict[str, str] | None = None,
) -> ValidationOutcome:
    """Run every command (stopping at the first failure) without a shell."""
    outcome = ValidationOutcome(passed=True)
    for command in commands:
        started = time.monotonic()
        try:
            argv = split_command(command)
            resolved = shutil.which(argv[0], path=(env or os.environ).get("PATH")) or argv[0]
            proc = subprocess.run(
                [resolved, *argv[1:]],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                env={**os.environ, **(env or {})},
                stdin=subprocess.DEVNULL,
            )
            exit_code = proc.returncode
            output = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            output = f"timed out after {timeout_seconds}s\n" + str(exc.stdout or "")[-500:]
        except (OSError, ValueError) as exc:
            exit_code = 127
            output = f"could not run command: {exc}"
        outcome.results.append(
            {
                "command": command,
                "exit_code": exit_code,
                "duration_seconds": round(time.monotonic() - started, 2),
                "output_tail": output[-OUTPUT_TAIL_CHARS:],
            }
        )
        if exit_code != 0:
            outcome.passed = False
            break
    return outcome
