"""Operator example: map the runner stdin prompt contract onto a print-mode CLI.

The runner always writes the role prompt to stdin and never interpolates it
into a shell string. Some coding-agent CLIs expect the prompt as an argv
value after `--`, treat `--add-dir` as variadic (so a trailing prompt would
be eaten), and hang if stdin is a pipe they keep polling.

CLIs that do not read piped stdin, or that exceed OS argv length limits on
large role prompts, can instead take the prompt from a file. Set
`BUILD_COORDINATOR_PROMPT_FILE_FLAG` to that flag (for example
`--prompt-file`). The wrapper writes a temporary prompt file and passes it
as an argument; the runner stdin contract is unchanged.

This wrapper is trusted operator configuration, not coordinator domain logic.
Edit COMMAND to the installed executable. Do not put vendor names into
coordinator service code.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


# Trusted operator argv. Override with BUILD_COORDINATOR_PRINT_CLI as a
# space-separated command. Vendor binaries belong in operator config, not
# coordinator domain logic. Example:
#   claude -p --output-format text --dangerously-skip-permissions
COMMAND = os.environ.get("BUILD_COORDINATOR_PRINT_CLI", "agent --print").split()
PROMPT_FILE_FLAG = os.environ.get("BUILD_COORDINATOR_PROMPT_FILE_FLAG", "").strip()


def main() -> int:
    prompt = sys.stdin.read()
    result_path = os.environ.get("BUILD_COORDINATOR_RESULT_PATH")
    cmd = list(COMMAND)
    if result_path:
        if os.environ.get("BUILD_COORDINATOR_RESULT_ADD_DIR", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            cmd.extend(["--add-dir", str(Path(result_path).parent)])
        prompt += (
            "\n\nMANDATORY: write one JSON object to "
            f"{result_path} using schema_version 1 and identity fields from "
            "BUILD_COORDINATOR_EXECUTION_ID, BUILD_COORDINATOR_TASK_ID, and "
            "BUILD_COORDINATOR_ROLE. The runner ignores stdout; the JSON file "
            "is the only lifecycle result. Do not persist secrets or hidden reasoning."
        )
    prompt_file = None
    if PROMPT_FILE_FLAG:
        handle, prompt_file = tempfile.mkstemp(
            prefix="build-coordinator-prompt-",
            suffix=".txt",
            text=True,
        )
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(prompt)
        cmd.extend([PROMPT_FILE_FLAG, prompt_file])
    else:
        cmd.extend(["--", prompt])
    try:
        completed = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            shell=False,
            cwd=os.getcwd(),
        )
        return int(completed.returncode)
    finally:
        if prompt_file:
            try:
                os.remove(prompt_file)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
