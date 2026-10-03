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
