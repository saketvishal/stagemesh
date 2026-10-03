# StageMesh vNext

StageMesh is a provider-neutral control plane for autonomous and multi-agent software engineering. It owns task lifecycle, durable state, validation, review, and integration while executors remain replaceable.

Initial lifecycle:

```text
PLAN -> IMPLEMENT -> VALIDATE -> REVIEW -> INTEGRATE -> DONE
```

This repository is a clean vNext implementation with SQLite persistence, deterministic recovery, exact-SHA candidates and evidence, provider-aware routing, task-source synchronization, machine-enforced change contracts, isolated task worktrees, and an invariant test suite.

Implementation agents run in per-task Git worktrees, not the shared checkout. Validation, review, and integration all bind their evidence to the exact candidate SHA; a task cannot reach `DONE` unless the candidate passes its contract, validation gates, independent review, and integration prerequisites.

## Quick Start

```bash
python -m pip install -e ".[dev]"
stagemesh init --project .
stagemesh doctor
stagemesh continue --once   # one coordinator pass; plain `continue` supervises one task to completion
stagemesh status
pytest
python scripts/invariants.py
python scripts/acceptance.py
```

Runtime state lives in `.stagemesh/stagemesh.sqlite3` by default.

## Provider pools and task isolation

StageMesh picks providers at run time instead of one fixed provider per stage. Each stage (`IMPLEMENT`, `REVIEW`) has a pool
(default `codex, claude, grok`; a `routing.stage_routes` entry is simply tried first). Set `routing.pools` to pin an explicit list:

```json
{ "routing": { "pools": { "IMPLEMENT": ["codex", "claude", "grok"], "REVIEW": ["claude", "grok", "codex"] },
              "provider_failure_cooldown_seconds": 900 } }
```

A provider is skipped (with the reason logged to stderr) when its CLI is not callable, it lacks the stage capability, it failed for
the same task and stage within the cooldown (this is how auth and quota failures are remembered), or, for `REVIEW` with
`require_independent_review`, it produced the candidate or shares its command. Failures fall through the pool automatically; if no
independent reviewer exists StageMesh refuses and lists every provider with its skip reason. Implementation always runs in an
isolated git worktree and the integration ref only advances after validation, independent review and integration pass.

### Provider registry and selection policies

`codex`, `claude` and `grok` are built-in registry entries; any other name can be added under `providers`, as a plain command string
or an object with `command`, `capabilities` (`IMPLEMENT`, `REVIEW`), optional `priority` (lower first) and `weight`:

```json
{ "providers": { "my-agent": { "command": "my-agent run", "capabilities": ["IMPLEMENT"] },
                 "reviewer-bot": { "command": "reviewer-bot review", "capabilities": ["REVIEW"] } },
  "routing": { "pools": { "IMPLEMENT": ["codex", "claude", "grok", "my-agent"], "REVIEW": ["claude", "grok", "reviewer-bot"] },
               "provider_selection_policy": "round_robin", "provider_weights": { "claude": 2, "grok": 2 } } }
```

`provider_selection_policy` ranks the *eligible* providers of a stage (after CLI, capability, cooldown and independence checks):
`priority` (default: priority, then pool order), `round_robin` (next after the provider that last answered for the stage),
`least_recently_used` (never-used first, then oldest answer) or `weighted` (lowest `(uses+1)/weight` first). The remaining providers
form the fallback chain in the same order. `STAGEMESH_<NAME>_CMD` overrides any provider command, `--provider` pins implementation,
and SINGLE_AGENT mode still uses its one provider.

## Choosing the next task

Plain `stagemesh continue` runs the only eligible task, or ranks several and runs the best one. `--task <id>` bypasses ranking and
is how a stale task is retried; `--choose` asks on the terminal. Ranking (best first): priority label, preferred label
(`stagemesh:prep`, `prep`, `governance`, `readiness`), valid contract over one needing auto-planning, then issue number. Tasks with an
excluded label (`stagemesh:blocked`, `stagemesh:deferred`), unmet dependencies, a stale failure (failed integration or pending
remediation) or no contract that can be auto-planned are skipped. The reason is logged and returned as `selection` in `--json`.
Tune it with:

```json
{ "task_selection": { "auto_select": true, "tie_breaker": "issue_number",
                    "priority_labels": ["priority:p0", "priority:p1", "priority:p2", "priority:p3"],
                    "preferred_labels": ["stagemesh:prep"], "excluded_labels": ["stagemesh:blocked"] } }
```

## Project profiles

A project that cannot be validated by guessing root-level test commands (a monorepo) ships `.stagemesh/profile.json`: task types
(prep, frontend, backend, schema, full), their gates, allowed/forbidden files and size limits, and label behavior. Auto-planning
builds contracts from it, `stagemesh profile --task <id>` shows what a task would get, and a hand-written contract still wins.
See `docs/profiles.md` and the Caventra profile in `docs/profiles/caventra.md`.

