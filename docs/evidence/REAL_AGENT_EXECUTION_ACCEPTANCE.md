# Real Coding-Agent Execution Acceptance Evidence (SM-012)

## Status: LIVE RUN COMPLETE

Every parallel run before this task exercised the SubprocessExecutor adapter
against a scripted stand-in worker (`tests/_scripted_worker.py`). This
evidence record covers what SM-012 adds: a live, reproducible run that
proves the same adapter contract against a **real** coding-agent CLI
(`claude`), routed only through `.stagemesh/project.yaml` worker templates
plus the operator's installed CLI, for all three roles -- BUILDER, REVIEWER,
and INTEGRATION -- plus deterministic coverage that a malformed or missing
result file is a typed failure rather than a silent success.

`tests/test_real_agent_end_to_end.py::test_real_agent_builder_reviewer_integration_on_scratch_repo`
was executed live against a real, authenticated `claude` CLI on 2026-09-26.
It passed. The durable evidence it produced is committed at
`docs/evidence/real_agent_runs/SM-012/{builder,reviewer,integration}-evidence.json`
in this repository (not left in a pytest tmp directory) and is summarized
below.

---

## How the run was routed (no manual worktree or worker choice)

The test copies `examples/public_dogfood/real_agent_end_to_end_demo.yaml`
into a scratch repository's `.stagemesh/project.yaml` (substituting the
manifest's literal `PYTHON_EXECUTABLE`/`INTEGRATION_AGENT_SCRIPT` path
placeholders for this machine's real interpreter and script path -- a plain
path substitution, not `${VAR}` shell-style expansion; see the comment in
that manifest), then loads it with
`build_coordinator.project.definition.load_project` and expands its worker
templates with `build_coordinator.project.runtime.build_runner_config` --
the exact functions StageMesh itself uses to turn a project's `workers:`
templates into `WorkerConfig` objects. The resulting `WorkerConfig.command`
values (not anything hand-constructed in the test) are what get executed for
all three roles:

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
- **INTEGRATION** declares `adapter: subprocess` with a literal `command:`
  array pointing at `tests/_real_agent_integration_worker.py`, run under
  this machine's real Python interpreter. This is **also** a real `claude`
  CLI subprocess, not StageMesh's built-in git integrator: the script drives
  `claude` headlessly with permission to run `git checkout`/`git merge`,
  instructs it to merge the reviewed commit into `main` with
  `git merge --no-ff`, and then -- mirroring the "an agent's self-report is
  never the lifecycle result" discipline `build_coordinator/agents/wrapper.py`
  uses for BUILDER/REVIEWER -- independently re-derives from git state
  whether the merge actually happened before ever writing
  `status: SUCCEEDED`. A bespoke script is used here (instead of routing
  through `build_coordinator/agents/wrapper.py` via `runtime: claude`)
  because that wrapper never special-cases the INTEGRATION role: its
  generic prompt tells the agent not to touch git history at all, and its
  result derivation reports a `feature_sha` from HEAD, never a
  `merge_commit_sha`. Editing `build_coordinator/agents/wrapper.py` or
  `build_coordinator/runner/orchestrator.py` is outside this task's allowed
  edit paths (`examples/`, `docs/`, `tests/` only), so an INTEGRATION-aware
  operator script was the honest way to get a real third coding-agent
  subprocess without weakening BUILDER/REVIEWER's existing contract.
- The reviewer worker id (`reviewer-1`) differs from the builder worker id
  (`builder-1`), satisfying independent review.

Because INTEGRATION now resolves to `adapter: subprocess` rather than
`builtin-git`, `BuildRunner._integration_succeeded`
(`build_coordinator/runner/orchestrator.py`) requires the project's
`auto_push_allowed` policy before completing the task. The test sets
`BUILD_COORDINATOR_AUTO_PUSH_ALLOWED=true` for the scratch scenario; the
scratch repo has no upstream remote configured, so nothing is actually
pushed anywhere -- this only lets the orchestrator transition a real
subprocess integration to `DONE`, matching the policy a real project with a
third-party INTEGRATION worker would need to set. The test does not rely on
the orchestrator's transition alone: it independently re-reads `git log` on
the scratch repo's `main` branch after the run and asserts the reported
`merge_commit_sha` is really reachable from it.

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
3. **INTEGRATION**, a real `claude` execution driven by
   `tests/_real_agent_integration_worker.py`, was given permission to run
   `git checkout`/`git merge` in the integration worktree, merged the
   reviewed commit into `main` with `git merge --no-ff`, and the script
   verified from git state (parent SHAs of the resulting commit, and that
   `main` actually advanced) that the merge really happened before reporting
   `SUCCEEDED`. Both the reviewed feature commit and the merge commit are
   present in the scratch repo's `main` history.
4. Provider, runtime, subprocess command, exit code, and result-file
   contents for each of the three executions were captured and committed as
   durable evidence (see below), not just asserted in-process or left in a
   pytest tmp directory.

### Evidence summary (see the committed JSON files for full detail)

| Role | Worker id | Provider | Adapter | Exit code | Result |
| --- | --- | --- | --- | --- | --- |
| BUILDER | `builder-1` | `anthropic` | `subprocess` | `0` | `SUCCEEDED`, `feature_sha=fe62bc9772b5b35a41ff9852adb473a9b99f1e8d` |
| REVIEWER | `reviewer-1` | `anthropic` | `subprocess` | `0` | `SUCCEEDED`, verdict `GREEN`, `ready_for_integration=true` |
| INTEGRATION | `integration-1` | `anthropic` | `subprocess` | `0` | `SUCCEEDED`, `merge_commit_sha=62a4aea8691a6611d36f0313d24854d7ef065db7` |

Full command argv, and each execution's raw structured result payload, are
recorded per-role in
`docs/evidence/real_agent_runs/SM-012/{builder,reviewer,integration}-evidence.json`.

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
