"""How a validation gate is launched and how its failure is explained.

Two jobs, both pure and deterministic (no provider, no network):

* Executable resolution. A gate command whose first element is an explicit path (`.venv/Scripts/python.exe`, `../tools/run`) is
  resolved against the gate's working directory, because Windows resolves a relative executable against the *parent's* directory
  before it applies `cwd`. The path is only normalised, never `resolve()`d: a virtual environment's `python` is a symlink or a
  launcher whose identity (`sys.prefix`) depends on the path it was started through. An explicit executable that does not exist is
  a launch failure; it is never replaced by a same-named program found on PATH. A bare name (`npm`, `python`) is looked up on PATH
  as before and the lookup is recorded.
* Timeout explanation. A timeout with an empty log proves nothing, so the class says exactly what the captured output supports:
  `service_unavailable` (the output names an unreachable service), `collection_timeout` and `setup_timeout` (the run never reached
  test execution), `test_execution_timeout` (tests were running; a candidate defect is plausible) or `no_output_timeout` (no
  evidence either way: the cause is not established and must not be claimed).
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .redaction import redact_output

TAIL_CHARS = 2000
PROGRESS_LINES = 6

TIMEOUT_SERVICE_UNAVAILABLE = "service_unavailable"
TIMEOUT_COLLECTION = "collection_timeout"
TIMEOUT_SETUP = "setup_timeout"
TIMEOUT_TEST_EXECUTION = "test_execution_timeout"
TIMEOUT_NO_OUTPUT = "no_output_timeout"
TIMEOUT_UNCLASSIFIED = "timeout_unclassified"

# Classes whose output points away from the candidate's code. They stop implementation retries at once and require a diagnosis
# first. `no_output_timeout` is deliberately absent: an empty log proves nothing either way, so it keeps the ordinary single
# remediation attempt and a repeated equivalent timeout stops (the cause is reported as not established, never guessed).
NON_CODE_TIMEOUTS = frozenset({TIMEOUT_SERVICE_UNAVAILABLE, TIMEOUT_COLLECTION, TIMEOUT_SETUP})

_SERVICE_MARKERS = re.compile(
    r"connection refused|could not connect|econnrefused|connection timed out|no route to host|name or service not known|"
    r"getaddrinfo failed|temporary failure in name resolution|cannot connect to the docker daemon|"
    r"database \"[^\"]+\" does not exist|role \"[^\"]+\" does not exist|password authentication failed|"
    r"(?:psycopg2?|asyncpg|sqlalchemy)\S*operationalerror|server closed the connection unexpectedly|"
    r"is the server running on host",
    re.IGNORECASE,
)
_SETUP_EVIDENCE = re.compile(r"\b(?:fixtures?|conftest|session[- ]?(?:scope|setup|start)|setup|setting up|migrat\w+)\b", re.IGNORECASE)
_COLLECTING = re.compile(r"\bcollecting\b", re.IGNORECASE)
_COLLECTED = re.compile(r"\bcollected\s+(\d+)\s+items?\b", re.IGNORECASE)
_TEST_PROGRESS = re.compile(
    r"::\S+\s+(?:PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b|\[\s*\d+%\]|^\s*[\.sFEx]{2,}\s*(?:\[|$)|^\s*(?:ok|not ok)\s+\d+\b",
    re.IGNORECASE | re.MULTILINE,
)
_IS_PATH_COMMAND = re.compile(r"[\\/]")


@dataclass(frozen=True)
class ExecutableResolution:
    """What was declared, what was launched and how the second came from the first."""

    declared: str
    executed: str | None
    method: str  # explicit-relative | explicit-absolute | path-lookup | bare-unresolved | missing-explicit
    detail: str = ""
    path: str | None = None  # the normalised location an explicit executable names, even when nothing exists there

    @property
    def launchable(self) -> bool:
        return self.method != "missing-explicit"

    def to_dict(self) -> dict[str, Any]:
        return {
            "declared": self.declared, "executed": self.executed, "method": self.method,
            **({"detail": self.detail} if self.detail else {}), **({"path": self.path} if self.path else {}),
        }


def is_explicit_executable(value: str) -> bool:
    return bool(_IS_PATH_COMMAND.search(value)) or Path(value).is_absolute()


def _pathext_candidates(candidate: Path) -> list[Path]:
    if os.name != "nt" or candidate.suffix:
        return [candidate]
    extensions = [ext.lower() for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if ext]
    return [candidate, *(candidate.with_name(candidate.name + ext) for ext in extensions)]


def resolve_gate_executable(command: list[str], run_dir: Path, *, search_path: str | None = None) -> ExecutableResolution | None:
    """Resolve `command[0]` for a gate that will run in `run_dir`. None for an empty command."""
    if not command:
        return None
    declared = command[0]
    if not is_explicit_executable(declared):
        found = shutil.which(declared) if search_path is None else shutil.which(declared, path=search_path)  # lets "npm" find npm.cmd on Windows without a shell
        if found:
            return ExecutableResolution(declared, found, "path-lookup")
        return ExecutableResolution(declared, None, "bare-unresolved", "not found on PATH; the launcher reports the failure")
    raw = Path(declared)
    absolute = raw.is_absolute()
    candidate = raw if absolute else Path(os.path.normpath(os.path.join(run_dir, declared)))  # normpath keeps symlinks/launchers as written
    for option in _pathext_candidates(candidate):
        if option.is_file():
            return ExecutableResolution(declared, str(option), "explicit-absolute" if absolute else "explicit-relative", path=str(option))
    return ExecutableResolution(
        declared,
        None,
        "missing-explicit",
        f"explicit executable {declared!r} does not exist at {candidate}; no PATH program was substituted",
        str(candidate),
    )


def bounded_output(text: str | None, limit: int = TAIL_CHARS) -> tuple[str, bool]:
    """The redacted tail of `text` and whether anything was cut."""
    if not text:
        return "", False
    cleaned = redact_output(text)
    return (cleaned[-limit:], True) if len(cleaned) > limit else (cleaned, False)


def progress_excerpt(*streams: str | None, limit: int = PROGRESS_LINES) -> list[str]:
    """The last non-empty output lines across the streams: what the gate was doing when it stopped."""
    lines = [line.strip()[:240] for stream in streams if stream for line in redact_output(stream).splitlines() if line.strip()]
    return lines[-limit:]


def classify_timeout(stdout: str | None, stderr: str | None) -> dict[str, Any]:
    """Explain a gate timeout from what it printed; the explanation never goes beyond that output."""
    text = f"{stdout or ''}\n{stderr or ''}"
    progress = progress_excerpt(stdout, stderr)
    if not text.strip():
        return {
            "timeout_class": TIMEOUT_NO_OUTPUT,
            "attribution": "undetermined",
            "explanation": (
                "the gate printed nothing before the timeout, so the cause is not established; this is not evidence of a database, "
                "environment or code problem. Run the gate command directly with verbose output to find where it stalls"
            ),
            "progress": progress,
        }
    marker = _SERVICE_MARKERS.search(text)
    started_tests = bool(_TEST_PROGRESS.search(text))
    if marker and started_tests:
        # Tests ran: a logged "connection refused" only counts if it is among the last lines, i.e. what the run was stuck on.
        marker = _SERVICE_MARKERS.search("\n".join(text.strip().splitlines()[-3:]))
    if marker:
        return {
            "timeout_class": TIMEOUT_SERVICE_UNAVAILABLE,
            "attribution": "infrastructure",
            "explanation": f"the output reports an unavailable service ({marker.group(0)[:80]!r}); no candidate change can fix that",
            "progress": progress,
        }
    collected = _COLLECTED.search(text)
    if started_tests:
        return {
            "timeout_class": TIMEOUT_TEST_EXECUTION,
            "attribution": "candidate_possible",
            "explanation": "tests were executing when the time limit was reached; the last progress lines show where",
            "progress": progress,
        }
    if _COLLECTING.search(text) and not collected:
        return {
            "timeout_class": TIMEOUT_COLLECTION,
            "attribution": "environment_or_import_time",
            "explanation": "the run was still collecting tests (importing test modules); a slow import or unavailable resource is probable",
            "progress": progress,
        }
    if collected and _SETUP_EVIDENCE.search(text.split(collected.group(0), 1)[-1]):
        return {
            "timeout_class": TIMEOUT_SETUP,
            "attribution": "environment_or_fixture_setup",
            "explanation": (
                f"collection finished ({collected.group(1)} items) and the output after it names fixture or session setup, but no test "
                "reported a result; setup did not complete, which points at an external resource or fixture rather than assertions"
            ),
            "progress": progress,
        }
    return {
        "timeout_class": TIMEOUT_UNCLASSIFIED,
        "attribution": "undetermined",
        "explanation": "the output does not identify the phase that was running; the cause is not established",
        "progress": progress,
    }
