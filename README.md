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

`stagemesh init` creates the project-local provider registry by detecting supported CLIs on `PATH` (or an explicit command environment override) without launching them. The registry is a snapshot: rerunning init does not replace operator edits, arbitrary command providers can be added under `providers`, and an explicitly empty registry stays empty. Detection or registration does **not** authenticate, authorize, or invoke a provider; unattended use still requires the separate `authorization.unattended_providers` opt-in.

For a fresh GitHub checkout, `stagemesh init` also detects `origin`, configures the GitHub task source, and reads open issues. When `STAGEMESH_GITHUB_TOKEN` has Issues write permission, it classifies only issues that have no labels: issues with a valid explicit contract or a deterministic auto-plan become `stagemesh:ready`; issues missing safe scope or validation information become `stagemesh:needs-triage`. Existing labels and explicit task-source policy are never replaced. The classification and any failure are recorded in the audit ledger, and the command resyncs successful labels before returning. Initialization still makes zero provider calls and does not start coding; use `stagemesh run-window` after authorizing the intended providers.

For StageMesh development, bootstrap the project-owned provider pool and run the fast synthetic stabilization gate before broader acceptance:

```bash
python scripts/bootstrap_stagemesh.py
stagemesh init --project .
python scripts/stabilization_gate.py
# Final, authoritative local gate (writes bounded machine-readable evidence):
python scripts/run_regression.py --report .stagemesh/regression-report.json
```

The stabilization gate drives the real coordinator against a fake provider CLI. It tests bounded recovery without calling real providers. The complete runner proves its audited lane union equals canonical pytest collection, isolates workers, propagates failures, and records exact counts and timings. See [test architecture and regression evidence](docs/test-architecture-redesign.md). Hosted Windows and Linux remain separate merge-readiness gates.

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

For larger programs, provider-neutral [objective governance](docs/objective-governance.md) can freeze an append-only plan, dependency graph, independent challenge, revision reconciliation, and outcome evaluation as workspace-isolated evidence. These artifacts govern decomposition and review; they do not make an objective runnable or prove that its work was executed.

## Validation, CI, and operator access

StageMesh applies a versioned repository-quality policy to changed Python and fails closed on newly introduced lint, complexity, strict-typing, dead-code, or type-escape violations. Candidate promotion independently inspects the exact baseline-to-candidate diff against the frozen task contract; blocking scope or policy violations preserve the commit under a quarantine ref instead of discarding it. See [code quality](docs/code-quality-gate.md) and [atomic scope and quarantine](docs/atomic-scope-and-quarantine.md).

Pull requests remain the authoritative Windows and Linux merge-readiness gate. Local focused checks support development, while `python scripts/run_regression.py --report .stagemesh/regression-report.json` produces the complete local regression artifact. Mixed Python/TypeScript policy enforcement has bounded synthetic qualification, not general production-readiness proof. `stagemesh ci-optimize-plan` can make a bounded, read-only batch-size recommendation from explicit queue and failure measurements; distributed cache service, live hosted-runner telemetry, and speculative execution are not yet implemented. See [development readiness](docs/development-readiness-qualification.md) and [CI optimization](docs/ci-optimization.md).

`stagemesh ui` and `stagemesh serve` expose the operator surface on loopback with a per-launch credential and read-only defaults; bounded control actions require explicit enablement. Remote hosting remains disabled. The reviewed direction is a single-tenant TLS reverse proxy with OIDC/PKCE and separate project-scoped read/control permissions, but implementation still requires operator-selected infrastructure, identity, secret storage, backup ownership, and independent security review. See the [operator UI contract](docs/operator-ui-contract.md) and [remote-hosting security plan](docs/remote-hosting-security.md).

## Providers, skills, MCP, and plugins

StageMesh selects eligible providers at runtime from project-owned pools. It ships adapter definitions for Codex, Claude, Grok, and Agy, but initialized projects route only providers recorded in their local registry; projects may register arbitrary command providers and stage-specific commands. Provider registration, executable availability, explicit authorization, live acceptance, and recorded invocation are separate states. Stage compatibility, independent-review identity, cooldown, and optional live acceptance are evaluated before dispatch. A provider failure falls through to the next eligible registered provider within configured budgets.

`stagemesh skills --json` inventories the five installed, pinned automatic skills, and `stagemesh skills --task <id> --json` previews deterministic stage selection without launching a provider. Projects inventory additional skills, MCP definitions, and skills-only Claude plugin packages in `.stagemesh/capabilities.json`. Registration is not invocation: `stagemesh capabilities --json` reports registry availability, while `stagemesh capabilities --task <id> --json` distinguishes attached, activated, loaded, invoked, and content-integrity evidence. Capability contracts opt in by ID, and dispatch refuses relevant policy or source-pin drift.

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
- [Bundled skills and proof of use](docs/agent-skills.md)
- [Provider readiness](docs/provider-readiness.md)
- [Objective governance](docs/objective-governance.md)
- [Development-readiness qualification](docs/development-readiness-qualification.md)
- [Repository quality policy](docs/code-quality-gate.md)
- [Atomic scope and quarantine](docs/atomic-scope-and-quarantine.md)
- [CI optimization boundaries](docs/ci-optimization.md)
- [Operator UI contract](docs/operator-ui-contract.md)
- [Remote-hosting security plan](docs/remote-hosting-security.md)
- [Workspace and task recovery](docs/workspace-and-task-recovery.md)
- [Reliable delivery](docs/reliable-delivery.md)
- [README information-architecture review](docs/readme-review.md)
