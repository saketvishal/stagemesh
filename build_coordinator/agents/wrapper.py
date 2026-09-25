"""Drive a real coding agent for one StageMesh execution.

    python -m build_coordinator.agents.wrapper --runtime <runtime-id>

StageMesh launches this as a worker command: the role prompt arrives on stdin,
the working directory is the task's isolated worktree, and the identity/result
paths arrive in environment variables. The wrapper turns the runner's structured
prompt into plain engineering instructions, runs the agent headlessly, and then
writes the result file itself from *deterministic facts* (git state, the
agent's final message), so an agent's self-report is never the lifecycle result.

The agent is told not to commit: the wrapper commits, which keeps git metadata
writes outside the agent's sandbox (linked worktrees keep it in the main repo).
Failures are classified into the provider-failure taxonomy so routing can move
work to another runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from build_coordinator.agents.profiles import PROFILES
from build_coordinator.execution.process_tree import attach_started_process, popen_kwargs

RESULT_SCHEMA_VERSION = 1
# Build/test byproducts that are never part of a deliverable, even when a project forgot to ignore them.
GENERATED_ARTIFACT_EXCLUDES = (
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.pyc",
    ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob)**/node_modules/**",
)
DEFAULT_TIMEOUT_SECONDS = 3000

_FAILURE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "AUTH_FAILURE",
        (
            "not logged in",
            "please run /login",
            "log in again",
            "login required",
            "unauthorized",
            "invalid api key",
            "not authenticated",
            "re-authenticate",
        ),
    ),
    (
        "QUOTA_EXHAUSTED",
        (
            "usage limit",
            "quota",
            "credit balance",
            "out of credits",
            "limit reached",
            "upgrade your plan",
            "balance exhausted",
            "usage balance",
            "payment required",
            "status 402",
        ),
    ),
    (
        "RATE_LIMITED",
        (
            "rate limit",
            "session limit",
            "429",
            "too many requests",
            "overloaded",
            "try again later",
            "resets ",
        ),
    ),
    (
        "NETWORK_FAILURE",
        ("econnreset", "econnrefused", "enotfound", "network error", "connection refused", "getaddrinfo", "could not resolve"),
    ),
)


def classify_failure(output: str) -> str:
    lowered = output.lower()
    for failure, needles in _FAILURE_PATTERNS:
        if any(needle in lowered for needle in needles):
            return failure
    return "EXECUTION_FAILURE"


def git(cwd: str, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (proc.stdout or "").strip()


def trusted_git_env(cwd: str) -> dict[str, str]:
    """Trust only this task worktree and its own repository, nothing broader.

    A sandboxed agent process may run under a different Windows identity than
    the one that owns the worktree; without this git would refuse to operate."""
    trusted = [Path(cwd).resolve().as_posix()]
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        repo = Path(common)
        trusted.append((repo.parent if repo.name == ".git" else repo).resolve().as_posix())
    env = {"GIT_CONFIG_COUNT": str(len(trusted))}
    for index, path in enumerate(trusted):
        env[f"GIT_CONFIG_KEY_{index}"] = "safe.directory"
        env[f"GIT_CONFIG_VALUE_{index}"] = path
    return env


def _bullets(items: Any) -> str:
    return "\n".join(f"  - {item}" for item in (items or [])) or "  (none)"


def render_prompt(role: str, raw: str, *, base_ref: str, reviewed_sha: str | None) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    definition = payload.get("task_definition") or {}
    envelope = payload.get("task_envelope") or {}
    title = definition.get("title") or envelope.get("title") or ""
    head = (
        f"TASK {definition.get('task_id') or envelope.get('task_id')}: {title}\n\n"
        f"Objective and requirements:\n{definition.get('description') or '(see title)'}\n\n"
        f"Acceptance criteria:\n{_bullets(definition.get('acceptance_criteria'))}\n"
    )
    if definition.get("permitted_scope"):
        head += f"\nStay within these paths:\n{_bullets(definition['permitted_scope'])}\n"
    if definition.get("implementation_notes"):
        head += f"\nNotes:\n{definition['implementation_notes']}\n"
    validation = definition.get("required_validation") or []

    if role == "REVIEWER":
        return (
            "You are an independent code reviewer. You did not write this change and you must not modify anything.\n\n"
            + head
            + f"\nThe commit under review is {reviewed_sha}, already checked out (detached) in this worktree. "
            f"Inspect the change with `git diff {base_ref}...HEAD` and read the surrounding code. Judge it against the "
            "objective and every acceptance criterion; look for correctness bugs, missing requirements, missing or weak "
            "tests, scope creep, security problems and unrelated changes.\n"
            + (f"\nStageMesh separately runs these validation commands, so you need not:\n{_bullets(validation)}\n" if validation else "")
            + "\nEnd your reply with ONE fenced ```json block, exactly this shape:\n"
            '{"verdict": "GREEN" | "GREEN_WITH_NOTES" | "REMEDIATION_REQUIRED", "findings": [...], '
            '"required_remediation": [...], "architecture_notes": [...], "ready_for_integration": true|false}\n'
            "Rules: GREEN / GREEN_WITH_NOTES need ready_for_integration=true and an empty required_remediation; "
            "REMEDIATION_REQUIRED needs ready_for_integration=false and concrete required_remediation items. "
            "Do not approve work you could not verify.\n"
        )

    prior = []
    for label, key in (
        ("Failures from earlier attempts (validation output / review findings)", "known_failures"),
        ("Open review blockers", "blockers"),
        ("Remaining work", "remaining_work"),
        ("Already completed", "completed_work"),
    ):
        values = envelope.get(key) or []
        if values:
            prior.append(f"{label}:\n{_bullets(values)}")
    resume = ("\nThis is a continuation of earlier work on this branch.\n" + "\n".join(prior) + "\n") if prior else ""
    return (
        "You are a senior software engineer working autonomously in an isolated git worktree (the current directory).\n\n"
        + head
        + resume
        + (
            "\nAfter you finish, StageMesh itself runs these validation commands in this worktree and the task only "
            f"proceeds if they pass. Run them yourself and fix failures until they pass:\n{_bullets(validation)}\n"
            if validation
            else ""
        )
        + "\nRules:\n"
        "  - Implement the task completely, in this worktree only; keep the change focused on the objective.\n"
        "  - Add or update tests where the task calls for them.\n"
        "  - Do NOT run git commit, push, checkout, reset, rebase or branch commands: StageMesh commits your working-tree changes.\n"
        "  - Do not write secrets, tokens or credentials into files. Do not modify files outside this worktree.\n"
        "  - If the task cannot be completed, leave the tree unchanged and explain why in your final message.\n"
    )


def parse_verdict(text: str) -> dict[str, Any] | None:
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates = blocks[::-1] or re.findall(r"(\{[^{}]*\"verdict\"[^{}]*\})", text, flags=re.DOTALL)[::-1]
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and str(data.get("verdict", "")).upper() in {
            "GREEN",
            "GREEN_WITH_NOTES",
            "REMEDIATION_REQUIRED",
        }:
            return data
    return None


def write_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def run_agent(cmd: list[str], prompt: str, *, cwd: str, timeout: float, env: dict[str, str]) -> tuple[int, str]:
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        **popen_kwargs(),
    )
    tree = attach_started_process(proc.pid)
    try:
        out, _ = proc.communicate(prompt, timeout=timeout)
        return proc.returncode, out or ""
    except subprocess.TimeoutExpired:
        tree.terminate()
        proc.kill()
        out, _ = proc.communicate()
        return 124, (out or "") + f"\nagent timed out after {timeout}s"
    finally:
        tree.close()


def _safe_write_stdout_tail(output: str, max_chars: int = 4000) -> None:
    tail = output[-max_chars:] if max_chars else output
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        try:
            buf.write(tail.encode("utf-8", errors="replace") + b"\n")
            buf.flush()
            return
        except Exception:
            pass
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        sys.stdout.write(tail + "\n")
    except UnicodeEncodeError:
        sys.stdout.write(tail.encode(encoding, errors="replace").decode(encoding, errors="replace") + "\n")
    try:
        sys.stdout.flush()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(prog="stagemesh-agent")
    parser.add_argument("--runtime", required=True)
    args = parser.parse_args(argv)
    profile = PROFILES.get(args.runtime)
    if profile is None or not profile.headless:
        print(f"runtime {args.runtime!r} cannot be driven headlessly", file=sys.stderr)
        return 2

    role = os.environ["BUILD_COORDINATOR_ROLE"].upper()
    role_key = "BUILDER" if role == "REMEDIATION" else role
    result_path = Path(os.environ["BUILD_COORDINATOR_RESULT_PATH"])
    identity = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "execution_id": os.environ["BUILD_COORDINATOR_EXECUTION_ID"],
        "task_id": os.environ["BUILD_COORDINATOR_TASK_ID"],
        "role": role,
    }
    cwd = os.getcwd()
    base_ref = os.environ.get("STAGEMESH_MAIN_REF", "main")
    reviewed = os.environ.get("BUILD_COORDINATOR_REVIEWED_FEATURE_SHA") or None
    timeout = float(os.environ.get("STAGEMESH_AGENT_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    prompt = render_prompt(role_key, sys.stdin.read(), base_ref=base_ref, reviewed_sha=reviewed)

    if role_key == "REVIEWER" and reviewed:
        checkout = subprocess.run(["git", "checkout", "--detach", reviewed], cwd=cwd, capture_output=True, text=True)
        if checkout.returncode != 0:
            write_result(result_path, {**identity, "status": "FAILED", "detail": f"could not check out {reviewed}"})
            return 1

    env = {**os.environ, **trusted_git_env(cwd)}
    with tempfile.TemporaryDirectory(prefix="stagemesh-agent-") as scratch:
        last_message = str(Path(scratch) / "last-message.txt")
        cmd = profile.command(
            role_key, cwd, model=os.environ.get("STAGEMESH_AGENT_MODEL") or None, last_message=last_message
        )
        code, output = run_agent(cmd, prompt, cwd=cwd, timeout=timeout, env=env)
        final = output
        if Path(last_message).is_file():
            final = Path(last_message).read_text(encoding="utf-8", errors="replace")
    # Agent output is arbitrary Unicode; a narrow console encoding (cp1252) must never crash the wrapper
    # after the agent has finished, so write bytes with replacement.
    _safe_write_stdout_tail(output, max_chars=4000)

    if code != 0:
        failure = "EXECUTION_FAILURE" if code == 124 else classify_failure(output)
        write_result(
            result_path,
            {**identity, "status": "FAILED", "provider_failure": failure, "detail": output[-300:], "runtime": args.runtime},
        )
        return 1

    if role_key == "REVIEWER":
        verdict = parse_verdict(final) or parse_verdict(output)
        if verdict is None:
            write_result(
                result_path,
                {**identity, "status": "FAILED", "provider_failure": "EXECUTION_FAILURE", "detail": "reviewer produced no parseable verdict"},
            )
            return 1
        write_result(
            result_path,
            {
                **identity,
                "status": "SUCCEEDED",
                "reviewed_feature_sha": reviewed,
                "verdict": str(verdict["verdict"]).upper(),
                "findings": list(verdict.get("findings") or []),
                "required_remediation": list(verdict.get("required_remediation") or []),
                "architecture_notes": list(verdict.get("architecture_notes") or []),
                "ready_for_integration": bool(verdict.get("ready_for_integration")),
            },
        )
        return 0

    # builder / remediation: derive the result from git, not from the agent
    if git(cwd, "status", "--porcelain"):
        git(cwd, "add", "-A", "--", ".", *GENERATED_ARTIFACT_EXCLUDES)
        subprocess.run(
            [
                "git", "-c", "user.name=StageMesh", "-c", "user.email=stagemesh@localhost",
                "commit", "-q", "-m", f"{identity['task_id']}: agent changes ({args.runtime})",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
    head = git(cwd, "rev-parse", "HEAD")
    base = git(cwd, "merge-base", "HEAD", base_ref)
    commits = git(cwd, "rev-list", f"{base}..HEAD").split()
    files = git(cwd, "diff", "--name-only", f"{base}..HEAD").splitlines()
    blockers = [] if commits else ["the agent produced no changes on the task branch"]
    write_result(
        result_path,
        {
            **identity,
            "status": "SUCCEEDED",
            "feature_sha": head,
            "files_changed": files,
            "commits_created": commits,
            "tests": [],
            "scope_expansion_required": False,
            "blockers": blockers,
            "runtime": args.runtime,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
