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
from build_coordinator.runner.git_safety import resolve_git_identity, resolve_git_identity_args
from build_coordinator.execution.process_tree import attach_started_process, popen_kwargs

RESULT_SCHEMA_VERSION = 1
# Build/test byproducts that are never part of a deliverable, even when a project forgot to ignore them.
GENERATED_ARTIFACT_EXCLUDES = (
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.pyc",
    ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob)**/node_modules/**",
    ":(exclude,glob)**/tmp/**",
)
DEFAULT_TIMEOUT_SECONDS = 3000

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(bearer)\s+([a-z0-9._~+/=-]{12,})"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*([^\s,;]+)"),
)

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
            "rate limit exceeded",
            "rate limited",
            "rate_limit_exceeded",
            "http 429",
            "status 429",
            "429 too many requests",
            "too many requests",
            "session limit",
        ),
    ),
    (
        "UNAVAILABLE",
        (
            "overloaded",
            "temporarily unavailable",
            "service unavailable",
            "capacity",
            "provider unavailable",
            "server busy",
            "status 503",
            "http 503",
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


def sanitize_diagnostic(text: str, *, max_chars: int = 300) -> str:
    detail = text[-max_chars:]
    for pattern in _SECRET_PATTERNS:
        detail = pattern.sub(r"\1 <redacted>", detail)
    return detail


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
    name, email = resolve_git_identity(cwd)
    env["GIT_AUTHOR_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = email
    env["GIT_COMMITTER_NAME"] = name
    env["GIT_COMMITTER_EMAIL"] = email
    return env


def _bullets(items: Any) -> str:
    return "\n".join(f"  - {item}" for item in (items or [])) or "  (none)"


def _finding_bullets(entries: Any) -> str:
    lines = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        lines.append(
            f"  - id={entry.get('id')} attempts={entry.get('attempts')} "
            f"first_seen={entry.get('first_seen_cycle')}: {entry.get('description')}"
        )
    return "\n".join(lines) or "  (none)"


def render_prompt(role: str, raw: str, *, base_ref: str, reviewed_sha: str | None) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    definition = payload.get("task_definition") or {}
    envelope = payload.get("task_envelope") or {}
    resume_context = payload.get("resume_context") or {}
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
        prior_findings = resume_context.get("open_findings_from_prior_review") or []
        disposition_section = ""
        if prior_findings:
            disposition_section = (
                "\nThese findings from prior review cycles are still tracked as open on this task:\n"
                f"{_finding_bullets(prior_findings)}\n"
                "For EVERY one of those ids, include an entry in `finding_dispositions` classifying it as "
                "RESOLVED, STILL_OPEN, INVALID, or NOT_APPLICABLE, with a `reason` when you change its prior "
                "status (for example when marking it RESOLVED, or when reopening one you previously closed). "
                "Do not silently drop an id -- an id missing from `finding_dispositions` and not restated in "
                "`findings` is treated as resolved.\n"
            )
        return (
            "You are an independent code reviewer. You did not write this change and you must not modify anything.\n\n"
            + head
            + f"\nThe commit under review is {reviewed_sha}, already checked out (detached) in this worktree. "
            f"Inspect the change with `git diff {base_ref}...HEAD` and read the surrounding code. Judge it against the "
            "objective and every acceptance criterion; look for correctness bugs, missing requirements, missing or weak "
            "tests, scope creep, security problems and unrelated changes.\n"
            + (f"\nStageMesh separately runs these validation commands, so you need not:\n{_bullets(validation)}\n" if validation else "")
            + disposition_section
            + "\nEnd your reply with ONE fenced ```json block, exactly this shape:\n"
            '{"verdict": "GREEN" | "GREEN_WITH_NOTES" | "REMEDIATION_REQUIRED" | "REVIEW_ENVIRONMENT_BLOCKED", "findings": [...], '
            '"finding_dispositions": [{"id": "...", "status": "RESOLVED"|"STILL_OPEN"|"INVALID"|"NOT_APPLICABLE", "reason": "..."}, ...], '
            '"required_remediation": [...], "architecture_notes": [...], "ready_for_integration": true|false}\n'
            "Rules: GREEN / GREEN_WITH_NOTES need ready_for_integration=true and an empty required_remediation; "
            "REMEDIATION_REQUIRED needs ready_for_integration=false and concrete required_remediation items; "
            "REVIEW_ENVIRONMENT_BLOCKED needs ready_for_integration=false, empty required_remediation, and findings explaining the environment or tooling failure. "
            "Do not request source-code remediation for review environment or tooling failures. Do not approve work you could not verify.\n"
        )

    open_findings = resume_context.get("open_findings") or []
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
    if open_findings:
        prior.append(f"Open findings to remediate (do not re-fix already-resolved findings):\n{_finding_bullets(open_findings)}")
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
    fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    candidates: list[str] = []
    for block in fenced[::-1]:
        stripped = block.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            candidates.append(stripped)
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates.extend(blocks[::-1])
    candidates.extend(re.findall(r"(\{[^{}]*\"verdict\"[^{}]*\})", text, flags=re.DOTALL)[::-1])
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


_UNSET = object()


def run_agent(
    cmd: list[str],
    prompt: str | None = None,
    *,
    cwd: str,
    timeout: float,
    env: dict[str, str],
    stdin: Any = _UNSET,
) -> tuple[int, str]:
    input_text = prompt if stdin is _UNSET else stdin
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
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
        out, _ = proc.communicate(input_text, timeout=timeout)
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
        cmd, agent_stdin = profile.build_invocation(
            role_key,
            cwd,
            prompt,
            model=os.environ.get("STAGEMESH_AGENT_MODEL") or None,
            last_message=last_message,
        )
        code, output = run_agent(cmd, cwd=cwd, timeout=timeout, env=env, stdin=agent_stdin)
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
            {**identity, "status": "FAILED", "provider_failure": failure, "detail": sanitize_diagnostic(output), "runtime": args.runtime},
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
                "finding_dispositions": [
                    item for item in (verdict.get("finding_dispositions") or []) if isinstance(item, dict)
                ],
                "required_remediation": list(verdict.get("required_remediation") or []),
                "architecture_notes": list(verdict.get("architecture_notes") or []),
                "ready_for_integration": bool(verdict.get("ready_for_integration")),
            },
        )
        return 0

    # builder / remediation: derive the result from git, not from the agent
    if git(cwd, "status", "--porcelain"):
        git(cwd, "add", "-A", "--", ".", *GENERATED_ARTIFACT_EXCLUDES)
        identity_args = resolve_git_identity_args(cwd)
        subprocess.run(
            [
                "git", *identity_args,
                "commit", "-q", "-m", f"{identity['task_id']}: implement changes",
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
