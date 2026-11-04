# StageMesh vNext

StageMesh is a provider-neutral control plane for autonomous and multi-agent software engineering. It owns task lifecycle, durable state, validation, review, and integration while executors remain replaceable.

```text
PLAN -> IMPLEMENT -> VALIDATE -> REVIEW -> INTEGRATE -> DONE
```

Implementation agents run in per-task Git worktrees, not the shared checkout. Validation, review, and integration bind their evidence to the exact candidate SHA. A task cannot reach `DONE` until its contract, validation gates, independent review, and integration prerequisites pass.

## Quick start

Install the development checkout:

```bash
python -m pip install -e ".[dev]"
```

Or install the known-good stable package tag:

```bash
python -m pip install "stagemesh @ git+https://github.com/saketvishal/stagemesh.git@v0.5.0"
```

The reviewed Windows per-user installer creates an isolated runtime and Start Menu shortcut without administrator rights:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-stagemesh.ps1
```

The installer never enables a schedule implicitly. Its source-pin, plan, uninstall, and wheel-integrity behavior is documented in [project-local runtime](docs/project-local-runtime.md). See [versioning](docs/versioning.md) for the stable-tag workflow.

Initialize a project and run one bounded task loop:

```bash
stagemesh init --project .
stagemesh doctor
stagemesh continue
stagemesh status
```

For StageMesh development, bootstrap the project-owned provider pool and run the fast synthetic stabilization gate before broader acceptance:

```bash
python scripts/bootstrap_stagemesh.py --providers codex,claude,grok,agy
python scripts/stabilization_gate.py
```

The stabilization gate drives the real coordinator against a fake provider CLI. It tests bounded recovery without calling real providers. Full acceptance, invariants, and hosted CI remain separate merge-readiness gates.

Runtime state lives in `.stagemesh/stagemesh.sqlite3` by default. Do not edit it by hand.

## Operating model

Use `stagemesh run-window` for bounded unattended or production work. It applies preflight checks, journals the run, uses isolated task worktrees, and remains PR-only unless the project explicitly authorizes a merge policy. The normal operator flow is:

```bash
stagemesh doctor
stagemesh queue reconcile
stagemesh run-window
stagemesh doctor
```

See [operator flow](docs/operator-flow.md) for queue reconciliation, blocked work, provider cooldowns, survivor waves, and interrupted runs. See [run-window](docs/run-window.md) and [run-window budgets](docs/run-window-budgets.md) for options, stop rules, restart behavior, delivery, and merge policy.

Founder hands-off operation is opt-in per project. Its deterministic policy, capability boundaries, incident scenarios, evidence requirements, and remaining acceptance gaps are tracked in [founder hands-off](docs/founder-hands-off.md). The reversible Windows scheduler wrapper is documented in [continuous driver](docs/continuous-driver.md) and is never installed implicitly.

## Providers, skills, MCP, and plugins

StageMesh selects eligible providers at runtime from project-owned pools. `codex`, `claude`, `grok`, and `agy` are built in; projects may register other command providers and stage-specific commands. Provider availability, stage compatibility, independent-review identity, cooldown, and optional live acceptance are evaluated before dispatch. A provider failure falls through to the next eligible provider within configured budgets.

Projects inventory skills, MCP definitions, and skills-only Claude plugin packages in `.stagemesh/capabilities.json`. Registration is not invocation: `stagemesh capabilities --json` distinguishes declared, installed, enabled, adapter-supported, usable, attached, loaded, invoked, and content-integrity states. Capability contracts opt in by ID, and dispatch refuses relevant policy or source-pin drift.

Supported runtime adapters are deliberately narrow:

- read-only `SKILL.md` attachment;
- skills-only Claude plugin import without hooks, commands, agents, MCP, or LSP activation;
- bounded Codex stdio MCP with ambient servers disabled, declared read-only tools, and redacted invocation evidence.

Executable plugin components, unsupported provider MCP routes, unpinned content, and permission expansion fail closed. Discovery and vendoring do not enable or invoke capabilities. See [capabilities](docs/capabilities.md), [agent skills](docs/agent-skills.md), [provider readiness](docs/provider-readiness.md), and [provider-output security](docs/provider-output-security.md).

## Task selection, isolation, and parallel work

Plain `stagemesh continue` runs the only eligible task or ranks eligible work by project policy. Use `--task <id>` for a specific task. Exact-label scopes restrict admission across restarts; contradictory labels, invalid dependencies, objective roots, stale failures, and unplannable tasks remain visible but do not become runnable. See [label scopes](docs/label-scope.md).

`stagemesh continue --parallel N` gives each task a separate worktree and database connection. Provider concurrency, contract write scopes, protected files, exclusive resources, dependency order, and serialized integration prevent unsafe overlap. A task failure stops that task, not unrelated work.

For production operation, use `run-window`. Raw `queue-run` is a legacy execution surface and requires explicit unsafe opt-in. The complete admission, recovery, interruption, and reporting behavior is in [operator flow](docs/operator-flow.md) and [run-window](docs/run-window.md).

## Recovery and safety boundaries

StageMesh diagnoses validation, review, provider, and integration failures before spending another implementation attempt. Repeated failures stop at bounded thresholds; changed failures count as progress. Recovery choices, evidence, retry epochs, and terminal fences are documented in [recovery budgets](docs/recovery-budgets.md) and [reliable delivery](docs/reliable-delivery.md).

Every task worktree has one StageMesh owner at a time. Lifecycle boundaries verify the sealed HEAD, tracked content, candidate identity, and exact-candidate evidence. External mutation blocks the task; StageMesh does not reset, adopt, or silently reconcile the workspace. This is lifecycle-boundary detection, not cryptographic isolation from another process running as the same user.

Use `stagemesh task-doctor` before any repair. Contract rebinding, rebaselining, baseline advancement, survivor waves, ownership recovery, known gaps, and refusal rules are preserved in [workspace and task recovery](docs/workspace-and-task-recovery.md). Never repair task state by editing SQLite.

## Profiles, handoff, and delivery

Monorepos and projects that cannot infer root-level gates should commit `.stagemesh/profile.json`. Profiles define task types, gates, file scopes, size limits, and label behavior; a hand-written task contract still wins. See [profiles](docs/profiles.md) and the [Caventra profile](docs/profiles/caventra.md).

`stagemesh handoff export` writes a redacted, schema-validated JSON snapshot inside the project. It includes task, queue, execution, gate, contract-scope, and advisory next-command metadata while excluding prompts, gate environments, canonical contract JSON, configuration, and secret-looking values.

Projects remain PR-only unless they explicitly configure hosted or StageMesh-controlled merging. Required checks must be green for the exact reviewed candidate; missing, pending, skipped, failed, or uncertain checks never count as success. Concurrency, ancestry, repository identity, base movement, post-landing verification, and interrupted delivery all fail closed. See [run-window merge policy](docs/run-window.md#pr-merging) and [reliable delivery](docs/reliable-delivery.md).

## Documentation map

- [Current implementation and acceptance boundaries](docs/status.md)
- [Operator flow](docs/operator-flow.md)
- [Run-window reference](docs/run-window.md)
- [Capabilities, skills, MCP, and plugin safety](docs/capabilities.md)
- [Provider readiness](docs/provider-readiness.md)
- [Workspace and task recovery](docs/workspace-and-task-recovery.md)
- [Reliable delivery](docs/reliable-delivery.md)
- [README information-architecture review](docs/readme-review.md)
