# Real Coding-Agent Execution Acceptance Evidence (SM-012)

## Status: LIVE RUN COMPLETE

Every parallel run before this task exercised the SubprocessExecutor adapter
against a scripted stand-in worker (`tests/_scripted_worker.py`). This
evidence record covers what SM-012 adds: a live, reproducible run that
proves the same adapter contract against a **real** coding-agent CLI
(`claude`), routed only through `.stagemesh/project.yaml` worker templates
plus the operator's installed CLI, plus deterministic coverage that a
malformed or missing result file is a typed failure rather than a silent
success.

`tests/test_real_agent_end_to_end.py::test_real_agent_builder_reviewer_integration_on_scratch_repo`
was executed live against a real, authenticated `claude` CLI on 2026-09-26.
It passed. The durable evidence it produced is committed at
`docs/evidence/real_agent_runs/SM-012/{builder,reviewer,integration}-evidence.json`
in this repository (not left in a pytest tmp directory) and is summarized
below.

---

## How the run was routed (no manual worktree or worker choice)

The test copies `examples/public_dogfood/real_agent_end_to_end_demo.yaml`
verbatim into a scratch repository's `.stagemesh/project.yaml`, then loads it
with `build_coordinator.project.definition.load_project` and expands its
worker templates with `build_coordinator.project.runtime.build_runner_config`
-- the exact functions StageMesh itself uses to turn a project's `workers:`
templates into `WorkerConfig` objects. The resulting `WorkerConfig.command`
values (not anything hand-constructed in the test) are what get executed:

- **BUILDER** and **REVIEWER** templates declare `runtime: claude`. The
  project loader (`_resolve_runtime_template` in
  `build_coordinator/project/runtime.py`) resolves that to StageMesh's own
  agent wrapper command,
  `[sys.executable, "-m", "build_coordinator.agents.wrapper", "--runtime", "claude"]`,
  which in turn drives the real `claude` CLI found on PATH. This is the
  supported way to point a worker template at an operator-installed runtime
  without hardcoding a vendor binary name in project.yaml -- see the
  commentary in `examples/public_dogfood/real_agent_end_to_end_demo.yaml`
  for why a literal `["${SOME_ENV_VAR}"]` command array does **not** work
  (StageMesh's loader does not perform `${VAR}` shell-style expansion on
  command arrays; it passes each entry through
  `tuple(str(part) for part in template["command"])` verbatim).
- **INTEGRATION** declares `adapter: builtin-git`, which resolves to
  `build_coordinator.execution.git_integrator.GitIntegrationExecutor` --
  StageMesh's real (not scripted) deterministic merge step. Merging an
  already-reviewed commit is mechanical by design and does not use a model
  (see that module's docstring), so this is a real `git merge --no-ff`
  against the scratch repository's real `main` branch, not a third
  coding-agent subprocess.
- The reviewer worker id (`reviewer-1`) differs from the builder worker id
  (`builder-1`), satisfying independent review.

## What the live run proved

1. **BUILDER**, a real `claude` execution, created `NOTES.md` in the scratch
   repository; StageMesh's own wrapper (which commits on the agent's behalf,
   per the wrapper's documented contract) committed it. The resulting
   `feature_sha` is a real commit reachable from `git log` in the scratch
   repo.
2. **REVIEWER**, a real `claude` execution with a distinct worker id, was
   handed the builder's commit sha, inspected it, and returned a parsed
   verdict (`GREEN`, `ready_for_integration: true`) matching the reviewed
   sha.
3. **INTEGRATION**, StageMesh's real git integrator, merged the reviewed
   commit into `main` with `git merge --no-ff` and advanced `main` to the
   resulting merge commit; both the reviewed feature commit and the merge
   commit are present in the scratch repo's `main` history.
4. Provider, runtime, exit code, and result-file contents for each of the
   three executions were captured and committed as durable evidence (see
   below), not just asserted in-process or left in a pytest tmp directory.

### Evidence summary (see the committed JSON files for full detail)

| Role | Worker id | Provider | Runtime | Exit code | Result |
| --- | --- | --- | --- | --- | --- |
| BUILDER | `builder-1` | `anthropic` | `claude` | `0` | `SUCCEEDED`, `feature_sha=fe62bc9772b5b35a41ff9852adb473a9b99f1e8d` |
| REVIEWER | `reviewer-1` | `anthropic` | `claude` | `0` | `SUCCEEDED`, verdict `GREEN`, `ready_for_integration=true` |
| INTEGRATION | `integration-1` | `stagemesh` | `local` (builtin-git) | n/a (in-process) | `SUCCEEDED`, `merge_commit_sha=62a4aea8691a6611d36f0313d24854d7ef065db7` |

---

## Malformed and missing result files fail closed, not silently

`tests/test_subprocess_executor_hardening.py` and
`tests/test_real_agent_end_to_end.py` exercise both failure modes through the
real `SubprocessExecutor` code path (not a mock):

- A worker process that exits without ever creating the result file at
  `BUILD_COORDINATOR_RESULT_PATH` is observed as `status="FAILED"` with
  `failure_kind == "EXECUTOR_RESULT_INVALID_OR_MISSING"`.
- A worker process that writes non-JSON content to that path is observed as
  `status="FAILED"` with the same `failure_kind` and
  `detail == "subprocess wrote an invalid structured result file"`.

Neither case is ever reported as `SUCCEEDED`. This closes the acceptance
criterion: *"A malformed or missing result file is reported as a typed
failure, not silently accepted."* These two tests are deterministic and run
unconditionally (no real agent required).

## Reproducing the live run

The live scenario is opt-in and self-skips when no real `claude` CLI is on
PATH:

```
pytest tests/test_real_agent_end_to_end.py::test_real_agent_builder_reviewer_integration_on_scratch_repo -q
```

It writes/overwrites
`docs/evidence/real_agent_runs/SM-012/{builder,reviewer,integration}-evidence.json`
with the outcome of that specific run.

## Claims not made

- **Self-hosting proven:** not claimed; unrelated to this task.
