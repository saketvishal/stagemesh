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

