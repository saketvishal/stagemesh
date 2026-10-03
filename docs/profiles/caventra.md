# Caventra StageMesh profile

`docs/profiles/caventra.profile.json` is the StageMesh profile for the Caventra repository. Install it by copying it to
`C:\caventra\.stagemesh\profile.json` (that folder is runtime state and is not committed to Caventra; this copy is the source
of truth, and a test keeps it valid). See [profiles.md](../profiles.md) for the file format.

With the profile in place a Caventra task needs no hand-written contract: `stagemesh continue` generates
`.stagemesh/contracts/<issue>.json` from the issue labels and text, and StageMesh no longer stops with "no validation gate could
be detected". A hand-written contract in `.stagemesh/contracts/` still takes precedence.

Check what a task will get, without writing anything:

```
stagemesh --project C:\caventra profile --task 54        # type, evidence, gates, scope, limits
stagemesh --project C:\caventra profile --json
```

## Task types and validation gates

| Type | Validation | Allowed files | Gates (in order) | Limits |
|---|---|---|---|---|
| `prep` — readiness, governance, docs | light | `docs/**`, `README.md` | `docs-static-hygiene`, `docs-static-diff-check`, `docs-static-boundaries`, `docs-static-roadmaps` | 15 files / 1,500 lines |
| `frontend` — `apps/web-v2` UI | standard | `apps/web-v2/src/**`, `apps/web-v2/tests/**` | hygiene, diff-check, `frontend-install`, `frontend-vitest`, `frontend-ui-lint`, `frontend-typecheck` | 40 / 4,000 |
| `backend` — `apps/api` services and API | standard | `apps/api/app/**`, `apps/api/tests/**` | hygiene, diff-check, `backend-engineering-known-failures`, `backend-unit-tests`, `backend-auth-integration` | 40 / 4,000 |
| `schema` — models and Alembic migrations | standard | `apps/api/migrations/versions/**`, `apps/api/app/models/**`, `apps/api/tests/integration/**` | hygiene, diff-check, `backend-engineering-known-failures`, `schema-migration-tests`, `backend-auth-integration` | 20 / 2,500 |
| `full` — broad, cross-cutting or unclassified | full | API, web-v2, docs | everything above plus `full-acceptance-regression` | 60 / 6,000 |

What the gates are:

* `docs-static-hygiene` runs Caventra's own `scripts/quality_change.py --skip-external --base main` (secret literals, unexpected
  binaries, test skips, dependency/lockfile consistency, visual-baseline changes). `docs-static-diff-check` is `git diff --check`.
* `docs-static-boundaries` / `-roadmaps` run the modular-boundary, ratchet-enforcement and roadmap-document engineering tests:
  seconds, but real checks on repository structure and docs.
* `backend-engineering-known-failures` calls `quality_change.run_known_failure_command`, so Caventra's own
  `docs/engineering/known_failures.json` policy applies: it fails only on NEW engineering-test failures.
* The frontend gates are `npm clean-install --offline`, `vitest run`, `ui:lint` (the UI static gate) and `tsc --noEmit`.
* `full-acceptance-regression` is all of `tests/integration` and `tests/acceptance`.
* Database gates use only the local `caventra_test` database on `localhost:55432` (the `caventra_pg` container); the URLs are
  set explicitly per gate, so an ambient production `DATABASE_URL` is never used.
* `PYTHON` is `C:\caventra\.venv\Scripts\python.exe` on Windows (gate checkouts have no `.venv`); override with
  `STAGEMESH_PROFILE_PYTHON`. `BASE_REF` is `main`.

### Known baseline failures

`main` is not fully green, so the profile excludes (and a shrinking list is the only acceptable direction) these existing failures
rather than letting them block every task. A **new** failure still fails the gate.

* `backend-unit-tests` deselects 10 `tests/unit` tests (taxonomy boundary, matter-eval smoke, predicate-publication and
  reassessment-surface boundary tests).
* `full-acceptance-regression` deselects 6 integration tests that fail on `main` (candidate grounding gate, document reassessment,
  evaluation adversarial, q015 authority promotion, w2 person association) and 1 intermittent concurrency test
  (`test_case_guidance_concurrency`, observed both passing and failing on identical code).
* The 5 engineering ratchet failures are handled by Caventra's `known_failures.json`, not by the profile.

When a listed test is fixed, remove it from the gate in the profile (regenerate or edit the JSON).

## How the type is chosen

1. A `validation:full` / `risk:high` label forces `full`.
2. Otherwise the first of these that matches decides: **labels** (`area:frontend`, `area:backend`, `area:schema`, `stagemesh:prep`,
   plus aliases such as `frontend`, `api`, `migration`, `governance`, `readiness`), then keywords in the issue **title**, then
   keywords in the description.
3. One product area wins over prep; two or more product areas (`area:frontend` + `area:backend`) escalate to `full`.
4. Nothing matched: `full`. When StageMesh cannot tell what a task touches it runs the most validation.

Label new Caventra issues with an `area:*` label so the type never depends on keywords. A task that adds tables **and** service
code needs `area:schema` and `area:backend` (resolves to `full`) or `validation:full`; `backend` alone protects
`apps/api/migrations/**`.

## What can never be in a candidate

Every generated contract forbids (both `**/x` and root-level `x` forms): `.env*`, keys and certificates (`*.pem`, `*.key`, `*.p12`,
`*.pfx`, `id_rsa*`), anything named `*secret*` or `credentials*.json`, `data/**`, `private_matter_inputs/**`, `**/uploads/**`,
SQLite/DB files and their `-wal`/`-shm` companions, `*.bak`, `.stagemesh/**` (runtime DB and state), `.build-coordinator/**`,
`.caventra_test/**`, `.venv/**`, `node_modules`, `.npm-cache`, `__pycache__`, pytest/eval temp folders, `dist`, `.vite`,
local-evaluation inputs, and user/tool state (`.claude`, `.agents`, `.codex`, `.cursor`, `.gemini`, `.kiro`, `.kilo`,
`.codegraph`, `.vscode`, `.idea`, `.mcp.json`, `opencode.jsonc`, `AGENTS.md`, `GEMINI.md`, `.github/**`). Frontend, backend and
schema types additionally protect manifests, lockfiles, configs, test configuration, the API client, fixtures, e2e specs and the
ratchet baselines. `prep` tasks may not touch code at all.

## Label behavior for task selection

The profile's `task_selection` applies unless `config.json` defines its own:

* priority `priority:p0` > `p1` > `p2` > `p3`;
* **preferred before broad product work**: `stagemesh:prep`, `prep`, `governance`, `readiness`;
* **never auto-selected**: `stagemesh:blocked`, `stagemesh:deferred`;
* **stale failed tasks** (failed integration, pending remediation) are skipped until retried explicitly with `--task <id>`;
* a task with no hand-written contract is selectable only if the profile can plan it, so product tasks always have validation
  gates before they run;
* equal rank: lowest issue number.

## First execution target

**#54 — "Prepare Caventra for safe StageMesh execution"** (`priority:p0`, `stagemesh:ready`). It is a preparation task, resolves to
`prep` (light validation, docs only, no product code, no database), and is what the profile selects first. Run it with
`stagemesh --project C:\caventra continue --task 54` (set `STAGEMESH_GITHUB_TOKEN` first).

Then, in this order: #45 (backend plus schema work; it already has a hand-written contract), then further product issues.
#46 is already DONE in StageMesh (its GitHub issue is still open). #47 is `stagemesh:blocked` and is never selected until that
label is removed. Do not add `stagemesh:ready` to an issue until it is meant to run.
