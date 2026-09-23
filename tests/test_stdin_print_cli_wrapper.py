"""Provider-neutral stdin print-CLI wrapper: argv vs prompt-file delivery.

The runner always writes the role prompt to stdin. Some CLIs need that
prompt as `-- <text>` on argv; others reject piped stdin and/or overflow
OS argv limits, so they take `--prompt-file PATH` instead. This wrapper
is operator configuration, not coordinator domain logic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WRAPPER = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "stdin_print_cli_wrapper.py"
)
FAKE_CLI = Path(__file__).resolve().parent / "_fake_print_cli.py"


def _run_wrapper(env: dict[str, str], prompt: str) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.update(env)
    return subprocess.run(
        [sys.executable, "-P", str(WRAPPER)],
        input=prompt,
        text=True,
        capture_output=True,
        shell=False,
        env=merged,
        check=False,
    )


def test_default_passes_prompt_after_double_dash(tmp_path: Path):
    out = tmp_path / "captured.json"
    result = _run_wrapper(
        {
            "BUILD_COORDINATOR_PRINT_CLI": f"{sys.executable} -P {FAKE_CLI} {out}",
        },
        "hello from stdin",
    )
    assert result.returncode == 0, result.stderr
    captured = json.loads(out.read_text(encoding="utf-8"))
    assert captured["argv"][-2:] == ["--", "hello from stdin"]
    assert captured["prompt_file_text"] is None


def test_prompt_file_flag_writes_temp_file_instead_of_argv(tmp_path: Path):
    out = tmp_path / "captured.json"
    result = _run_wrapper(
        {
            "BUILD_COORDINATOR_PRINT_CLI": f"{sys.executable} -P {FAKE_CLI} {out}",
            "BUILD_COORDINATOR_PROMPT_FILE_FLAG": "--prompt-file",
            "BUILD_COORDINATOR_RESULT_PATH": str(tmp_path / "result.json"),
            "BUILD_COORDINATOR_EXECUTION_ID": "exec-1",
            "BUILD_COORDINATOR_TASK_ID": "TASK-1",
            "BUILD_COORDINATOR_ROLE": "PLANNER",
        },
        "planner prompt body",
    )
    assert result.returncode == 0, result.stderr
    captured = json.loads(out.read_text(encoding="utf-8"))
    assert "--" not in captured["argv"]
    assert captured["argv"][-2] == "--prompt-file"
    prompt_path = Path(captured["argv"][-1])
    assert captured["prompt_file_text"] is not None
    assert "planner prompt body" in captured["prompt_file_text"]
    assert str(tmp_path / "result.json") in captured["prompt_file_text"]
    assert "schema_version 1" in captured["prompt_file_text"]
    assert not prompt_path.exists(), "temp prompt file must be removed after the CLI exits"
