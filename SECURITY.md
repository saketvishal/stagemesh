# Security and Trust Boundaries

## What the coordinator trusts

| Input | Trust level | Rationale |
|---|---|---|
| `pyproject.toml` / `coordinator_config.py` | Trusted — operator-controlled | Written by the operator, not by agents |
| Runner config (YAML/JSON) | Trusted — operator-controlled | Defines worker commands, paths, credentials references |
| Task title, description, acceptance criteria | **Untrusted** | May originate from user input or model output |
| Worker result JSON | **Untrusted** — structurally validated | Workers are separate processes; coordinator validates schema |
| Worker stdout/stderr | **Not used** for lifecycle decisions | Only the result file at the runner-generated path is authoritative |

## What agents never control

- Which executable is run (set in operator-controlled runner config)
- Which working directory is used (set in operator-controlled runner config)
- Which paths are allowed (set in task `permitted_scope` by the operator)
- Whether a push is allowed (set by `auto_push_allowed` in runner config)
- Whether their own result is accepted (coordinator validates schema and identity fields)

## Secrets

Secrets must **never** appear in:
- Database rows (tasks, claims, checkpoints, events, executions)
- Coordinator config files (use `source: environment` or `source: provider_session`)
- Worker result JSON files
- Log output
- CLI arguments (use stdin for prompt delivery)

Runner config may reference environment variable names or provider session home
paths. It must not contain literal API keys, tokens, or passwords.

## Prompt injection

Task content (title, description, acceptance criteria) is delivered to agents as
part of a structured prompt but is never treated as system-level instructions
by the coordinator itself. Operators should be aware that task content reaches
agent execution context and may attempt prompt injection against the agent.
This is a known property of LLM-based systems; the coordinator does not
amplify or mitigate it beyond keeping task content out of coordinator logic.

## Worktree isolation

Workers are launched with `cwd=worktree_path` from operator-controlled runner
config. When `allowed_workspace_roots` is set in the runner config, any
worktree path not under those roots is rejected before launch.

Task-provided text cannot choose the working directory.

## Human gates

The following conditions escalate to a human and cannot be cleared by automation:

```
SCOPE_EXPANSION_REQUIRED
ARCHITECTURE_DECISION_REQUIRED
REMOTE_PUSH_APPROVAL_REQUIRED
MERGE_CONFLICT
MIGRATION_SCOPE_VIOLATION
SECURITY_POLICY_BLOCK
EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED
REMEDIATION_LIMIT_REACHED
TEST_FAILURE_REQUIRES_JUDGMENT
COORDINATOR_INVARIANT_FAILURE
REVIEWED_SHA_CHANGED
```

Human escalations are durable: they survive coordinator restarts and cannot
be cleared by re-running automation.

## Reporting vulnerabilities

Please report security vulnerabilities by opening a private issue or by
contacting the maintainers directly before public disclosure.