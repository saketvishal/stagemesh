"""Coding-agent runtime profiles and honest readiness probing.

A runtime is a way of running an agent (a CLI). It is separate from the provider
(who serves the model), the model, the worker (a StageMesh slot bound to one
runtime) and the capabilities a worker offers. Profiles here describe how to
detect and drive a runtime headlessly; nothing else in StageMesh names a
specific runtime, and additional runtimes can be added as data.

A runtime is READY only after a live headless probe returned the expected
answer, never merely because its executable exists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

READY = "READY"
NOT_INSTALLED = "NOT_INSTALLED"
NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
NOT_HEADLESS = "NOT_HEADLESS"
HEADLESS_FAILED = "HEADLESS_FAILED"
DISABLED = "DISABLED"
AUTHENTICATED = "AUTHENTICATED"  # logged in, but no live headless run has proven it

PROBE_PROMPT = "Reply with exactly the word READY and nothing else."

# Words that mean "this CLI has a non-interactive mode"; used for runtimes that
# only ship a GUI entry point so the limitation is evidence, not assumption.
HEADLESS_MARKERS = ("--print", "-p,", "--json", "--output", "--headless", "--no-window", "exec ")


@dataclass(frozen=True)
class RuntimeProfile:
    runtime_id: str
    provider: str
    display: str
    executables: tuple[str, ...]
    capabilities: tuple[str, ...]
    headless: bool = True
    version_args: tuple[str, ...] = ("--version",)
    auth_args: tuple[str, ...] | None = None
    auth_ok_markers: tuple[str, ...] = ()
    help_args: tuple[str, ...] = ("--help",)
    notes: str = ""

    def executable(self) -> str | None:
        for name in self.executables:
            found = shutil.which(name)
            if found:
                return found
        return None

    def command(self, role: str, cwd: str, *, model: str | None = None, last_message: str | None = None) -> list[str]:
        """argv for a headless run; the prompt is supplied on stdin."""
        exe = self.executable() or self.executables[0]
        if self.runtime_id == "codex":
            sandbox = "read-only" if role == "REVIEWER" else "workspace-write"
            cmd = [exe, "exec", "--ephemeral", "-C", cwd, "-s", sandbox]
            if model:
                cmd += ["-m", model]
            if last_message:
                cmd += ["-o", last_message]
            return [*cmd, "-"]
        if self.runtime_id == "claude":
            if role == "REVIEWER":
                allowed = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git status:*)"
                mode = "default"
            else:
                allowed = (
                    "Read,Edit,Write,Glob,Grep,"
                    "Bash(python:*),Bash(python3:*),Bash(pytest:*),Bash(npm:*),Bash(npx:*),Bash(node:*),"
                    "Bash(git status:*),Bash(git diff:*),Bash(git log:*),Bash(git show:*),"
                    "Bash(ls:*),Bash(cat:*),Bash(dir:*),Bash(mkdir:*)"
                )
                mode = "acceptEdits"
            cmd = [exe, "-p", "--output-format", "text", "--permission-mode", mode, "--allowedTools", allowed]
            if model:
                cmd += ["--model", model]
            return cmd
        raise ValueError(f"runtime {self.runtime_id!r} has no headless command")


PROFILES: dict[str, RuntimeProfile] = {
    "codex": RuntimeProfile(
        runtime_id="codex",
        provider="openai",
        display="OpenAI Codex CLI",
        executables=("codex",),
        capabilities=("CODING", "ADVANCED_REASONING", "CODE_REVIEW", "SECURITY_REVIEW"),
        auth_args=("login", "status"),
        auth_ok_markers=("logged in",),
    ),
    "claude": RuntimeProfile(
        runtime_id="claude",
        provider="anthropic",
        display="Claude Code",
        executables=("claude",),
        capabilities=("CODING", "ADVANCED_REASONING", "CODE_REVIEW", "SECURITY_REVIEW", "ARCHITECTURE"),
        auth_args=("auth", "status"),
        auth_ok_markers=('"loggedin": true',),
    ),
    "antigravity": RuntimeProfile(
        runtime_id="antigravity",
        provider="google",
        display="Antigravity IDE",
        executables=("antigravity-ide", "antigravity"),
        capabilities=("CODING",),
        headless=False,
        help_args=("chat", "--help"),
        notes="GUI IDE; `chat` opens a window and returns immediately",
    ),
}


@dataclass
class RuntimeStatus:
    runtime_id: str
    provider: str
    display: str
    state: str
    executable: str | None = None
    version: str | None = None
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    checked_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "provider": self.provider,
            "display": self.display,
            "state": self.state,
            "executable": self.executable,
            "version": self.version,
            "detail": self.detail,
            "evidence": self.evidence,
            "checked_at": self.checked_at,
        }

    @property
    def ready(self) -> bool:
        return self.state == READY


def _run(argv: list[str], *, timeout: float, cwd: str | None = None, stdin: str | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=None if stdin is not None else subprocess.DEVNULL,
        )
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)


def probe_runtime(profile: RuntimeProfile, *, live: bool = True, timeout: float = 150.0) -> RuntimeStatus:
    """Determine what is actually true about a runtime on this machine."""
    status = RuntimeStatus(profile.runtime_id, profile.provider, profile.display, NOT_INSTALLED)
    exe = profile.executable()
    if exe is None:
        status.detail = f"none of {', '.join(profile.executables)} found on PATH"
        return status
    status.executable = exe
    code, out = _run([exe, *profile.version_args], timeout=30)
    status.version = out.splitlines()[0][:120] if code == 0 and out else None

    if not profile.headless:
        code, help_text = _run([exe, *profile.help_args], timeout=30)
        status.evidence = {
            "command": " ".join([Path(exe).name, *profile.help_args]),
            "exit_code": code,
            "headless_markers_found": [m for m in HEADLESS_MARKERS if m in help_text],
            "help_excerpt": help_text[:400],
        }
        status.state = NOT_HEADLESS
        status.detail = (
            profile.notes or "no non-interactive mode"
        ) + "; StageMesh needs a headless run that returns a result"
        return status

    if profile.auth_args:
        code, out = _run([exe, *profile.auth_args], timeout=30)
        text = out.lower()
        if code != 0 or not any(marker in text for marker in profile.auth_ok_markers):
            status.state = NOT_AUTHENTICATED
            status.detail = f"not logged in ({' '.join([Path(exe).name, *profile.auth_args])}: {out[:160]})"
            return status

    if not live:
        status.state = AUTHENTICATED
        status.detail = "logged in; run `stagemesh agent setup` (without --quick) to prove it works headlessly"
        return status

    with tempfile.TemporaryDirectory(prefix="stagemesh-probe-") as scratch:
        subprocess.run(["git", "init", "-q"], cwd=scratch, capture_output=True)
        started = time.monotonic()
        cmd = profile.command("REVIEWER", scratch)
        code, out = _run(cmd, timeout=timeout, cwd=scratch, stdin=PROBE_PROMPT)
        elapsed = round(time.monotonic() - started, 1)
    status.evidence = {"live_probe_seconds": elapsed, "exit_code": code}
    if code == 0 and "READY" in out:
        status.state = READY
        status.detail = f"headless probe answered in {elapsed}s"
    else:
        status.state = HEADLESS_FAILED
        status.detail = f"headless probe failed (exit {code}): {out[-200:]}"
    return status


def discover_runtimes(*, live: bool = True, only: list[str] | None = None) -> list[RuntimeStatus]:
    return [
        probe_runtime(profile, live=live)
        for name, profile in PROFILES.items()
        if only is None or name in only
    ]
