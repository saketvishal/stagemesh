from __future__ import annotations

import filecmp
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .agent_skills import SkillPackError, is_frontend_task, skill_prompt
from .attribution import attribution_for_worker
from .capacity import CapacityKind, CapacityRegistry
from .config import StageMeshConfig
from .contract_binding import ContractRejected, bind_task_contract
from .domain import ExecutionKind, ExecutionStatus
from .execution import (
    PROVIDER_TIMEOUT,
    ExecutionResult,
    capacity_evidence,
    classify_failure,
    communicate_bounded,
    popen_session_kwargs,
    provider_timeout_seconds,
)
from .focused_validation import focused_test_prompt
from .git_identity import provider_environment
from .persistence import Store
from .process_identity import popen_identity
from .redaction import redact_command_secrets
from .remediation import remediation_context
from .research_context import build_research_brief
from .workspace_guard import (
    EXTERNAL_WORKSPACE_MUTATION,
    WorkspaceLease,
    WorkspaceMutation,
    owned_workspace,
)
from .workspaces import (
    NO_IMPLEMENTATION_CHANGE,
    WorkspaceEnvironmentError,
    commit_implementation_candidate,
    record_task_baseline,
)

MAX_EXECUTION_RESULT_CHARS = 200
PROVIDER_PERMISSION_DENIED = "provider_permission_denied"


IMPLEMENTATION_DISCIPLINE = (
    "Implementation discipline:\n"
    "- Make the smallest complete change that satisfies the task and the change contract; do not add future-proofing, optional "
    "features, wrappers, or broad refactors.\n"
    "- Before editing, identify the surfaces the change must touch: callers, tests, fixtures, config, docs, exports, and migrations. "
    "Do not edit outside those surfaces unless the contract explicitly allows it.\n"
    "- Prefer existing project helpers and patterns, then platform or standard library features, then installed dependencies. Do not add "
    "a dependency for a small local change.\n"
    "- Keep validation, error handling, security, accessibility, data-loss protection, and requested behavior intact. Never weaken a gate "
    "to make a change pass.\n"
    "- For non-trivial logic, leave a focused test, assertion, or runnable check that proves the behavior. Use the narrowest check that "
    "actually verifies the result.\n"
    "- If you discover useful work outside the contract, leave it out of the diff and mention it as deferred work.\n\n"
)

SPEC_PLANNING_DISCIPLINE = (
    "Spec planning gate:\n"
    "- Before editing, write a short private implementation spec in your scratch notes: user-visible behavior, affected surfaces, "
    "data/contracts, tests or checks, and risks.\n"
    "- Base that spec only on the change contract, project specs, and contextual research brief above. Do not invent requirements.\n"
    "- If the contract/specs are too vague to make a safe code change, stop and say what must be clarified instead of coding.\n"
    "- After the scratch spec is clear, implement only the smallest diff that satisfies it.\n\n"
)

FRONTEND_DESIGN_DISCIPLINE = (
    "Frontend design discipline:\n"
    "- Ground UI choices in the product, audience, and job of the screen instead of default SaaS chrome.\n"
    "- Before building new or reshaped UI, choose a compact design direction: palette, type scale, layout logic, and the one "
    "memorable element.\n"
    "- Avoid generic AI-page tells unless the brief explicitly asks for them: decorative gradients, identical card grids, all-caps "
    "eyebrows, arbitrary numbered labels, and scattered fade/slide animations.\n"
    "- Use visual structure as information: borders, labels, dividers, spacing, and numbers must explain the workflow, not decorate it.\n"
    "- Keep copy functional and consistent. Buttons should say exactly what happens, errors should say what failed and how to fix it, "
    "and empty states should point to the next action.\n"
    "- Preserve mobile responsiveness, visible keyboard focus, reduced-motion behavior, and accessible color contrast.\n\n"
)

MAX_SPEC_PROMPT_CHARS = 12000
MAX_SPEC_PROMPT_CHARS_PER_FILE = 5000


class ProviderValidationError(ValueError):
    pass


class ProviderAdapter(Protocol):
    name: str
    capabilities: frozenset[str]

    def check_capacity(self) -> str:
        ...

    def execute(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        ...


def resolve_executable(executable: str, provider_name: str | None = None) -> str | None:
    if not executable:
        return None
    found = shutil.which(executable)
    if found:
        return _complete_codex_executable(found)
    p = Path(executable)
    if p.name and p.name != executable:
        found = shutil.which(p.name)
        if found:
            return _complete_codex_executable(found)
    if p.stem and p.stem != executable and p.stem != p.name:
        found = shutil.which(p.stem)
        if found:
            return _complete_codex_executable(found)
    return None


def _complete_codex_executable(executable: str) -> str:
    """Keep a detached npm Codex binary with its identical packaged tool host."""
    path = Path(executable)
    if sys.platform != "win32" or path.name.casefold() != "codex.exe":
        return executable
    if path.with_name("codex-code-mode-host.exe").is_file():
        return executable
    package = path.parent / "node_modules" / "@openai" / "codex"
    patterns = (
        "node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe",
        "vendor/*/bin/codex.exe",
    )
    for pattern in patterns:
        for candidate in sorted(package.glob(pattern)):
            try:
                if candidate.with_name("codex-code-mode-host.exe").is_file() and filecmp.cmp(path, candidate, shallow=False):
                    return str(candidate)
            except OSError:
                continue
    return executable


@dataclass(frozen=True)
class RuntimeCommandAdapter:
    """SDK adapter for command-line coding agents such as Codex, Claude, or Grok."""

    name: str
    command: tuple[str, ...]
    capabilities: frozenset[str] = frozenset({"code", "review", "validate"})
    timeout_seconds: float | None = None
    # Optional stage-specific commands. `command` is the default; these replace it for one stage. The adapter is still ONE provider
    # (one `name`), so a candidate it implemented is never reviewed by it, whichever command each stage runs.
    implement_command: tuple[str, ...] | None = None
    review_command: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_name(self.name))
        object.__setattr__(self, "command", _validate_command(self.command))
        object.__setattr__(self, "capabilities", _validate_capabilities(self.capabilities))
        for field_name in ("implement_command", "review_command"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _validate_command(value))

    def command_for(self, capability: str) -> tuple[str, ...]:
        """The command this provider runs for a stage: "code" (implementation) or "review"; anything else uses the default."""
        cmd = self.command
        if capability == "code" and self.implement_command is not None:
            cmd = self.implement_command
        elif capability == "review" and self.review_command is not None:
            cmd = self.review_command
        if not cmd:
            return cmd
        exe = cmd[0]
        found = shutil.which(exe)
        if found:
            complete = _complete_codex_executable(found)
            return (complete, *cmd[1:]) if complete != found else cmd
        relocated = resolve_executable(exe, self.name)
        if relocated:
            return (relocated, *cmd[1:])
        return cmd

    def all_commands(self) -> tuple[tuple[str, ...], ...]:
        return tuple(dict.fromkeys(c for c in (self.command_for("code"), self.command_for("review"), self.command, self.implement_command, self.review_command) if c is not None))

    def check_capacity(self) -> str:
        cmd = self.command_for("code") if "code" in self.capabilities else self.command
        executable = cmd[0] if cmd else ""
        return CapacityKind.AVAILABLE if executable and shutil.which(executable) else CapacityKind.UNKNOWN

    def check_stage_capacity(self, capability: str) -> str:
        """Is the command this provider would run for that stage callable? (check_capacity() only looks at the default command.)"""
        command = self.command_for(capability)
        return CapacityKind.AVAILABLE if command and shutil.which(command[0]) else CapacityKind.UNKNOWN

    def execute(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        if self.check_stage_capacity("code") != CapacityKind.AVAILABLE:
            return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=True, failure_reason="provider_unavailable")
        try:
            with owned_workspace(store, project, task_id, ExecutionKind.IMPLEMENTATION, claim_id=claim_id) as lease:
                return self._execute_owned(store, task_id, claim_id, project, lease)
        except WorkspaceMutation:
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=EXTERNAL_WORKSPACE_MUTATION)
        except WorkspaceEnvironmentError as exc:
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=str(exc))

    def _execute_owned(self, store: Store, task_id: str, claim_id: str | None, project: Path, lease: WorkspaceLease) -> ExecutionResult:
        run_path = lease.path
        baseline_sha = record_task_baseline(store, task_id, run_path)
        try:
            bound = bind_task_contract(store, project, task_id, baseline_sha)
        except ContractRejected as exc:
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=f"{exc.reason}: {exc}")
        task = store.get_task(task_id)
        try:
            task_prompt = _build_task_prompt(
                task_id, task, run_path, contract=bound.contract, remediation=remediation_context(store, task_id, project)
            )
        except SkillPackError as exc:
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=f"skill_pack_unavailable: {exc}")
        command, provider_input = _command_and_input(self.command_for("code"), task_prompt)
        lease.check("before_agent")
        try:
            proc = subprocess.Popen(
                command,
                cwd=run_path,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=provider_environment(run_path),
                **popen_session_kwargs(),
            )
        except FileNotFoundError as exc:
            is_cap, reason = classify_failure(1, exc=exc)
            return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=is_cap, failure_reason=reason)
        identity = popen_identity(proc)
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=claim_id,
            kind=ExecutionKind.IMPLEMENTATION,
            actor=self.name,
            pid=identity.pid,
            process_create_time=identity.create_time,
            boot_id=identity.boot_id,
            executable=identity.executable,
        )
        lease.bind_execution(execution_id)
        try:
            stdout, stderr, timed_out = communicate_bounded(proc, provider_input, provider_timeout_seconds(self.timeout_seconds))
            lease.after_agent()
            if timed_out:
                store.finish_execution(execution_id, ExecutionStatus.FAILED, result=PROVIDER_TIMEOUT)
                return ExecutionResult(ExecutionStatus.FAILED, failure_reason=PROVIDER_TIMEOUT)
            if _missing_provider_tool_host(stderr):
                store.finish_execution(execution_id, ExecutionStatus.FAILED, result="provider_unavailable")
                output, _ = capacity_evidence("", stderr)
                return ExecutionResult(
                    ExecutionStatus.FAILED, capacity_failure=True,
                    failure_reason="provider_unavailable", provider_output=output,
                )
            if proc.returncode != 0:
                is_cap, reason = classify_failure(proc.returncode, stdout, stderr)
                store.finish_execution(execution_id, ExecutionStatus.FAILED, result=reason)
                output, retry_after = capacity_evidence(stdout, stderr)  # the operator sees why, capacity failure or not
                return ExecutionResult(
                    ExecutionStatus.FAILED,
                    capacity_failure=is_cap,
                    failure_reason=reason,
                    provider_output=output,
                    retry_after=retry_after,
                )
            sha = commit_implementation_candidate(
                store,
                task_id,
                run_path,
                baseline_sha,
                f"StageMesh implementation for {task_id}",
                attribution=attribution_for_worker("local-worker", self.name),
            )
            if sha is None:
                output, _ = capacity_evidence(stdout, stderr)
                failure_reason = _provider_permission_denial(stdout, stderr) or NO_IMPLEMENTATION_CHANGE
                store.finish_execution(execution_id, ExecutionStatus.FAILED, result=failure_reason)
                return ExecutionResult(
                    ExecutionStatus.FAILED,
                    failure_reason=failure_reason,
                    provider_output=output or None,
                )
            store.add_candidate(task_id, sha, self.name, durable_handoff=True)
            lease.seal(sha)
            store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
            return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)
        except Exception as exc:
            store.finish_execution(execution_id, ExecutionStatus.FAILED, result=_execution_result(exc))
            if _is_external_workspace_mutation(exc):
                return ExecutionResult(ExecutionStatus.FAILED, failure_reason=EXTERNAL_WORKSPACE_MUTATION)
            raise

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        return self.execute(store, task_id, claim_id, project)

    def review_candidate(self, prompt: str, project: Path, candidate_sha: str) -> str:
        if self.check_stage_capacity("review") != CapacityKind.AVAILABLE:
            return _review_infrastructure_failure("provider_unavailable")
        temp_dir = tempfile.mkdtemp(prefix="stagemesh-review-")
        review_path = Path(temp_dir) / "candidate"
        try:
            setup_error = _prepare_review_checkout(Path(project), review_path, candidate_sha)
            if setup_error:
                return setup_error
            before_head = _git_output(review_path, "rev-parse", "HEAD")
            if before_head != candidate_sha:
                return _review_infrastructure_failure("review workspace did not checkout exact candidate")
            try:
                review_prompt = prompt + skill_prompt(prompt, stage="review", project=review_path)
            except SkillPackError as exc:
                return _review_infrastructure_failure(f"skill_pack_unavailable: {exc}")
            command, provider_input = _command_and_input(self.command_for("review"), review_prompt, structured_review=True)
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=review_path,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=_review_provider_environment(Path(project), review_path),
                    **popen_session_kwargs(),
                )
            except FileNotFoundError:
                return _review_infrastructure_failure("provider_unavailable")
            stdout, stderr, timed_out = communicate_bounded(proc, provider_input, provider_timeout_seconds(self.timeout_seconds))
            if timed_out:
                return _review_infrastructure_failure(PROVIDER_TIMEOUT)
            after_head = _git_output(review_path, "rev-parse", "HEAD")
            tracked_dirty = _tracked_content_changed(review_path)
            if after_head != before_head or tracked_dirty:
                return _review_failure("review execution mutated candidate workspace")
            if _missing_provider_tool_host(stderr):
                output, _ = capacity_evidence("", stderr)
                return _review_infrastructure_failure("provider_unavailable", output)
            if proc.returncode != 0:
                is_cap, reason = classify_failure(proc.returncode, stdout, stderr)
                if is_cap:
                    output, retry_after = capacity_evidence(stdout, stderr)
                    return _review_infrastructure_failure(reason, output, retry_after)
                return _review_infrastructure_failure(reason)
            return _provider_text_response(stdout)
        finally:
            _cleanup_review_workspace(Path(temp_dir))


def _review_provider_environment(project: Path, review_path: Path) -> dict[str, str]:
    environment = provider_environment(project)
    # The sandbox account differs from the host account that created this exact
    # disposable checkout. Trust only this checkout, only in the review process.
    setting = "safe.directory=" + review_path.resolve().as_posix()
    quoted = "'" + setting.replace("'", "'\\''").replace("!", "'\\!'") + "'"
    inherited = environment.get("GIT_CONFIG_PARAMETERS", "")
    environment["GIT_CONFIG_PARAMETERS"] = (inherited + " " + quoted).strip()
    return environment


def _missing_provider_tool_host(stderr: str) -> bool:
    text = stderr.casefold()
    return "failed to spawn code-mode host" in text and (
        "host executable was not found" in text or "cannot find the file specified" in text
    )


@dataclass(frozen=True)
class RuntimeReviewAdapter:
    runtime: RuntimeCommandAdapter
    project: Path
    candidate_sha: str

    @property
    def name(self) -> str:
        return self.runtime.name

    def review(self, prompt: str) -> str:
        return self.runtime.review_candidate(prompt, self.project, self.candidate_sha)


REVIEW_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["PASS", "FAIL_WITH_FINDINGS", "REVIEW_INCOMPLETE"]},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string"},
                        "message": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["severity", "message"],
                    "additionalProperties": True,
                },
            },
        },
        "required": ["decision"],
        "additionalProperties": True,
    },
    separators=(",", ":"),
)


def _command_and_input(command: tuple[str, ...], prompt: str, *, structured_review: bool = False) -> tuple[list[str], str]:
    args = list(command)
    executable = Path(args[0]).name.lower() if args else ""
    if executable in {"grok", "grok.exe"}:
        args = _ensure_arg(args, "--json-schema", REVIEW_JSON_SCHEMA) if structured_review else args
        for flag in ("-p", "--single"):
            if flag in args:
                index = args.index(flag)
                if index == len(args) - 1 or args[index + 1].startswith("-"):
                    return [*args[: index + 1], prompt, *args[index + 1 :]], ""
                return args, ""
        return [args[0], "-p", prompt, *args[1:]], ""
    if executable in {"agy", "agy.exe"}:
        if structured_review:
            args = _ensure_arg(_ensure_arg(args, "--output-format", "json"), "--json-schema", REVIEW_JSON_SCHEMA)
        for flag in ("-p", "--print", "--prompt"):
            if flag in args:
                index = args.index(flag)
                if index == len(args) - 1 or args[index + 1].startswith("-"):
                    return [*args[: index + 1], prompt, *args[index + 1 :]], ""
                return args, ""
        return [args[0], "--print", prompt, *args[1:]], ""
    return args, prompt


def _ensure_arg(args: list[str], flag: str, value: str) -> list[str]:
    if flag in args:
        return args
    return [*args, flag, value]


def _provider_text_response(stdout: str) -> str:
    text = stdout.strip()
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return text
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"].strip()
    if isinstance(payload, dict) and isinstance(payload.get("structured_output"), dict):
        return json.dumps(payload["structured_output"], separators=(",", ":"))
    return text


def _provider_permission_denial(stdout: str, stderr: str) -> str | None:
    text = f"{stdout}\n{stderr}".lower()
    if "headless mode cannot prompt" in text and "auto-denied" in text:
        return PROVIDER_PERMISSION_DENIED
    if "tool required" in text and "permission" in text and "auto-denied" in text:
        return PROVIDER_PERMISSION_DENIED
    return None


def _prepare_review_checkout(project: Path, review_path: Path, candidate_sha: str) -> str | None:
    review_path.mkdir(parents=True, exist_ok=True)
    init = subprocess.run(["git", "init", "--quiet"], cwd=review_path, text=True, capture_output=True, check=False)
    if init.returncode != 0:
        detail = _bounded_process_output(init)
        return _review_infrastructure_failure("review workspace setup failed" + (f": {detail}" if detail else ""))

    # A sandboxed reviewer cannot read an alternate object directory outside
    # its workspace. Import candidate history locally so diff/log remain usable.
    imported = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-tags",
         "--no-recurse-submodules", "--no-auto-maintenance", project.resolve().as_uri(), candidate_sha],
        cwd=review_path, text=True, capture_output=True, check=False,
    )
    if imported.returncode != 0:
        detail = _bounded_process_output(imported)
        return _review_infrastructure_failure("review object import failed" + (f": {detail}" if detail else ""))

    # Reviewers must inspect the committed candidate bytes, not a platform-normalized
    # checkout shaped by the machine's global Git config.
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=review_path, text=True, capture_output=True, check=False)
    checkout = subprocess.run(
        ["git", "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", candidate_sha],
        cwd=review_path,
        text=True,
        capture_output=True,
        check=False,
    )
    if checkout.returncode != 0:
        detail = _bounded_process_output(checkout)
        return _review_infrastructure_failure("review candidate checkout failed" + (f": {detail}" if detail else ""))
    return None


def _source_object_store(project: Path) -> Path | None:
    result = subprocess.run(["git", "rev-parse", "--git-path", "objects"], cwd=project, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return None
    objects = Path(result.stdout.strip())
    if not objects.is_absolute():
        objects = project / objects
    return objects.resolve()


def _cleanup_review_workspace(path: Path) -> None:

    def retry_with_write_permission(function: object, cleanup_path: str, _exc_info: object) -> None:
        try:
            os.chmod(cleanup_path, stat.S_IREAD | stat.S_IWRITE)
            function(cleanup_path)  # type: ignore[operator]
        except OSError:
            pass

    for attempt in range(3):
        try:
            shutil.rmtree(path, onerror=retry_with_write_permission)
            return
        except OSError:
            if attempt < 2:
                time.sleep(0.1)
    # Review workspaces are throwaway checkouts. On Windows, provider tooling can leave cache files briefly locked
    # (for example .uv-cache), and that cleanup failure must not replace a completed review result.
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _execution_result(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_EXECUTION_RESULT_CHARS]


def _bounded_process_output(result: subprocess.CompletedProcess[str]) -> str:
    return " ".join((result.stderr or result.stdout or "").split())[:MAX_EXECUTION_RESULT_CHARS]


def _is_external_workspace_mutation(exc: Exception) -> bool:
    return getattr(exc, "code", None) == EXTERNAL_WORKSPACE_MUTATION


READ_ONLY_GIT_TOOLS = "Bash(git status*),Bash(git diff*),Bash(git log*),Bash(git show*),Bash(git rev-parse*)"

WRITE_CAPABLE_DEFAULT_COMMANDS = {
    "codex": "codex exec --sandbox workspace-write",
    "claude": (
        "claude -p --permission-mode acceptEdits --permission-prompts none "
        f"--allowedTools 'Edit,Write,MultiEdit,{READ_ONLY_GIT_TOOLS},Bash(python *),Bash(pytest *),Bash(ruff *)'"
    ),
    "grok": (
        "grok --permission-mode acceptEdits --always-approve "
        "--allow Edit --allow Write --allow MultiEdit "
        "--allow 'Bash(git status*)' --allow 'Bash(git diff*)' --allow 'Bash(git log*)' "
        "--allow 'Bash(git show*)' --allow 'Bash(git rev-parse*)' "
        "--allow 'Bash(python *)' --allow 'Bash(pytest *)' --allow 'Bash(ruff *)'"
    ),
    "agy": "agy --mode accept-edits",
}

LEGACY_DEFAULT_COMMANDS = {
    "codex": "codex exec",
    "claude": "claude -p",
    "grok": "grok",
    "agy": "agy",
}

PROVIDER_COMMAND_ENVS = {
    "codex": "STAGEMESH_CODEX_CMD",
    "claude": "STAGEMESH_CLAUDE_CMD",
    "grok": "STAGEMESH_GROK_CMD",
    "agy": "STAGEMESH_AGY_CMD",
}


def approved_default_adapters() -> list[RuntimeCommandAdapter]:
    commands = {
        name: os.environ.get(PROVIDER_COMMAND_ENVS[name]) or command
        for name, command in WRITE_CAPABLE_DEFAULT_COMMANDS.items()
    }
    return adapters_from_commands(commands)


def _write_capable_command(name: str, command: str) -> str:
    if name in LEGACY_DEFAULT_COMMANDS and shlex.split(command) == shlex.split(LEGACY_DEFAULT_COMMANDS[name]):
        return WRITE_CAPABLE_DEFAULT_COMMANDS[name]
    return command


def adapters_from_config(config: StageMeshConfig) -> list[RuntimeCommandAdapter]:
    commands = {adapter.name: shlex.join(adapter.command) for adapter in approved_default_adapters()}
    commands.update({name: _write_capable_command(name, command) for name, command in config.provider_commands.items()})
    adapters = adapters_from_commands(commands)
    # Object-form entries declare which stages a provider serves; plain strings and built-ins serve both.
    stage_caps = {"IMPLEMENT": "code", "REVIEW": "review"}
    resolved = []
    for adapter in adapters:
        spec = config.provider_specs.get(adapter.name)
        if spec is not None:
            capabilities = frozenset({"validate", *(stage_caps[stage] for stage in spec.capabilities)})
            adapter = RuntimeCommandAdapter(
                adapter.name,
                adapter.command,
                capabilities,
                adapter.timeout_seconds,
                implement_command=_stage_command(adapter.name, spec.implement_command),
                review_command=_stage_command(adapter.name, spec.review_command),
            )
        resolved.append(adapter)
    return resolved


def stage_command_summary(adapter: RuntimeCommandAdapter) -> dict[str, dict[str, object]]:
    """Per served stage: which command runs, whether it is the provider default or a stage override, and if it is callable."""
    summary: dict[str, dict[str, object]] = {}
    for stage, capability in (("IMPLEMENT", "code"), ("REVIEW", "review")):
        if capability not in adapter.capabilities:
            continue
        command = adapter.command_for(capability)
        configured_override = adapter.implement_command if capability == "code" else adapter.review_command
        override = configured_override is not None and configured_override != adapter.command
        summary[stage] = {
            "command": redact_command_secrets(shlex.join(command)),
            "executable": command[0],
            "source": "override" if override else "default",
            "available": adapter.check_stage_capacity(capability) == CapacityKind.AVAILABLE,
        }
    return summary


def _stage_command(name: str, command: str | None) -> tuple[str, ...] | None:
    if command is None:
        return None
    if len(command) > 2000:
        raise ProviderValidationError(f"stage command for {name} must be 2000 characters or fewer")
    try:
        return tuple(shlex.split(command))
    except ValueError as exc:
        raise ProviderValidationError(f"stage command for {name} is not valid shell syntax: {exc}") from exc


def adapters_from_commands(commands: dict[str, str]) -> list[RuntimeCommandAdapter]:
    if not isinstance(commands, dict):
        raise ProviderValidationError("provider commands must be an object")
    adapters: list[RuntimeCommandAdapter] = []
    for name, command in sorted(commands.items()):
        if not isinstance(command, str) or not command.strip():
            raise ProviderValidationError(f"provider command for {name} must be a non-empty string")
        if len(command) > 2000:
            raise ProviderValidationError(f"provider command for {name} must be 2000 characters or fewer")
        try:
            parts = tuple(shlex.split(command))
        except ValueError as exc:
            raise ProviderValidationError(f"provider command for {name} must be valid shell-style syntax") from exc
        adapters.append(RuntimeCommandAdapter(name=name, command=parts))
    return adapters


def record_provider_capacity(registry: CapacityRegistry, adapters: list[ProviderAdapter]) -> None:
    for adapter in adapters:
        registry.record(adapter.name, adapter.check_capacity())


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ProviderValidationError("provider adapter name must be a non-empty string")
    normalized = name.strip()
    if len(normalized) > 100:
        raise ProviderValidationError("provider adapter name must be 100 characters or fewer")
    if any(char.isspace() for char in normalized):
        raise ProviderValidationError("provider adapter name must not contain whitespace")
    return normalized


def _validate_command(command: tuple[str, ...]) -> tuple[str, ...]:
    if not command or any(not isinstance(part, str) or not part.strip() for part in command):
        raise ProviderValidationError("provider adapter command must contain non-empty arguments")
    normalized = tuple(part.strip() for part in command)
    if len(normalized) > 100:
        raise ProviderValidationError("provider adapter command must contain 100 arguments or fewer")
    if any(len(part) > 1000 for part in normalized):
        raise ProviderValidationError("provider adapter command arguments must be 1000 characters or fewer")
    return normalized


def _validate_capabilities(capabilities: frozenset[str]) -> frozenset[str]:
    if not capabilities:
        raise ProviderValidationError("provider adapter must declare at least one capability")
    normalized = frozenset(capability.strip() for capability in capabilities if isinstance(capability, str) and capability.strip())
    if len(normalized) != len(capabilities):
        raise ProviderValidationError("provider adapter capabilities must be non-empty strings")
    return normalized


def _build_task_prompt(
    task_id: str,
    task: object,
    project: Path | None = None,
    contract: object = None,
    remediation: dict[str, object] | None = None,
) -> str:
    """Build the prompt string sent via stdin to a provider CLI.

    The prompt gives the agent its task title and keeps Git ownership with
    StageMesh so candidate commits remain trustworthy.
    """
    import sqlite3 as _sqlite3

    from .contracts import ContractError, contract_prompt, load_contract

    row_keys = set(task.keys()) if isinstance(task, _sqlite3.Row) else set()
    title = task["title"] if "title" in row_keys else str(task_id)
    contract_text = ""
    effective_contract = contract
    frontend_text = _frontend_design_prompt(title, contract)
    spec_text = _project_specs_prompt(project, contract)
    research_text = build_research_brief(project, contract) if project is not None else ""
    if contract is not None:
        contract_text = "\n\n" + contract_prompt(contract) + "\n"
    elif project is not None:
        try:
            loaded_contract = load_contract(project, task_id)
            effective_contract = loaded_contract
            contract_text = "\n\n" + contract_prompt(loaded_contract) + "\n"
            frontend_text = _frontend_design_prompt(title, loaded_contract)
            spec_text = _project_specs_prompt(project, loaded_contract)
            research_text = build_research_brief(project, loaded_contract)
        except ContractError as exc:
            contract_text = f"\n\nChange contract is invalid and must be fixed before coding: {exc}\n"
    remediation_text = _remediation_prompt(remediation) if remediation else ""
    return (
        f"StageMesh task: {title}\n\n"
        f"{contract_text}"
        f"{spec_text}"
        f"{research_text}"
        f"{remediation_text}"
        "Please implement the changes described above.\n\n"
        f"{SPEC_PLANNING_DISCIPLINE}"
        f"{IMPLEMENTATION_DISCIPLINE}"
        f"{frontend_text}"
        f"{skill_prompt(title, effective_contract, remediation=bool(remediation), project=project)}"
        f"{focused_test_prompt(project, effective_contract)}"
        "Workspace rules:\n"
        "- Edit files only. Do not run git commit, checkout, switch, reset, rebase, merge, cherry-pick, branch, tag, "
        "amend, or any command that moves HEAD or rewrites history.\n"
        "- You may use read-only git commands such as status, diff, log, show, and rev-parse.\n"
        "- Leave the finished changes in the working tree. StageMesh owns all Git commits and will create the "
        "implementation candidate itself.\n"
        "- If no code change is needed, explain why and leave the tree unchanged.\n"
    )


def _frontend_design_prompt(title: str, contract: object | None) -> str:
    return FRONTEND_DESIGN_DISCIPLINE if is_frontend_task(title, contract) else ""


def _project_specs_prompt(project: Path | None, contract: object | None) -> str:
    spec_refs = getattr(contract, "spec_refs", ())
    if not spec_refs:
        return ""
    if project is None:
        return ""
    lines = ["", "Project-local specs from this installed project:"]
    remaining = MAX_SPEC_PROMPT_CHARS
    for ref in spec_refs:
        if not isinstance(ref, str):
            continue
        resolved = _resolve_project_spec(project, ref)
        if resolved is None:
            lines.extend([f"\n--- {ref} ---", "[spec ref rejected: path must stay inside the project]"])
            continue
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            lines.extend([f"\n--- {ref} ---", f"[spec ref could not be read: {exc}]"])
            continue
        excerpt = text[: min(remaining, MAX_SPEC_PROMPT_CHARS_PER_FILE)]
        remaining -= len(excerpt)
        lines.extend([f"\n--- {ref} ---", excerpt.rstrip()])
        if len(text) > len(excerpt):
            lines.append("[spec truncated for prompt size]")
        if remaining <= 0:
            lines.append("[remaining project specs omitted for prompt size]")
            break
    lines.append("\nUse these project specs as the source of truth. Do not invent StageMesh-owned product specs.")
    return "\n".join(lines) + "\n\n"


def _resolve_project_spec(project: Path, ref: str) -> Path | None:
    try:
        root = project.resolve()
        target = (root / ref).resolve()
    except OSError:
        return None
    if target == root or root not in target.parents:
        return None
    return target


def _remediation_prompt(remediation: dict[str, object]) -> str:
    lines = [
        "",
        f"Previous candidate {remediation['candidate_sha']} failed {remediation['stage']}.",
        "",
        f"Findings recorded against candidate {remediation['candidate_sha']} (verbatim; fix exactly these):",
        "",
    ]
    for finding in remediation["findings"][:50]:  # type: ignore[index]
        lines.append(f"- [{finding['severity']}] {finding['message']}")
    failure_details = remediation.get("failure_details")
    if isinstance(failure_details, list) and failure_details:
        lines.extend(
            [
                "",
                "Exact failed validation evidence (including bounded stdout/stderr; use it to repair the failure):",
                json.dumps(failure_details, indent=2, sort_keys=True),
            ]
        )
    diagnosis = remediation.get("diagnosis")
    if isinstance(diagnosis, dict):
        lines.extend(["", f"Diagnosis ({diagnosis.get('category')}): {str(diagnosis.get('summary'))[:500]}"])
        if diagnosis.get("provider_analysis"):
            lines.append(f"Independent analysis: {str(diagnosis['provider_analysis'])[:800]}")
    if remediation.get("recovery_note"):
        lines.extend(["", str(remediation["recovery_note"])])
    owner_state = remediation.get("task_owner_state")
    if isinstance(owner_state, dict):
        durable = {
            key: owner_state.get(key)
            for key in (
                "objective",
                "stage",
                "status",
                "baseline_sha",
                "integration_ref",
                "integration_sha",
                "candidate_sha",
                "accepted_work",
                "current_problem",
                "next_action",
                "unresolved_findings",
                "fixes_attempted",
                "recovery_history",
            )
        }
        lines.extend(["", "Durable StageMesh parent state for this task:", json.dumps(durable, indent=2, sort_keys=True)])
    lines.extend(
        [
            "",
            (
                "Fix only these findings while preserving the original task objective and the change contract above. "
                "Do not make unrelated changes."
            ),
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def _git_output(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _tracked_content_changed(path: Path) -> bool:
    unstaged = subprocess.run(["git", "diff", "--quiet"], cwd=path, check=False)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=path, check=False)
    return unstaged.returncode != 0 or staged.returncode != 0


def _review_infrastructure_failure(reason: str, output: str | None = None, retry_after: str | None = None) -> str:
    import json

    payload: dict[str, str] = {"decision": "INFRASTRUCTURE_FAILURE", "reason": reason}
    if output:
        payload["provider_output"] = output[:500]
    if retry_after:
        payload["retry_after"] = retry_after[:120]
    return json.dumps(payload)


def _review_failure(message: str) -> str:
    import json

    return json.dumps({"decision": "FAIL_WITH_FINDINGS", "findings": [{"severity": "error", "message": message}]})
