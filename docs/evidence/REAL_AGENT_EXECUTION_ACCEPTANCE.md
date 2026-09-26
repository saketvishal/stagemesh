# Real Coding-Agent Execution Acceptance Evidence (SM-012)

## Status: MECHANISM PROVEN, LIVE RUN PENDING OPERATOR EXECUTION

Every parallel run before this task exercised the SubprocessExecutor adapter
against a scripted stand-in worker (`tests/_scripted_worker.py`). This
evidence record covers what SM-012 adds: a reproducible, credential-free
mechanism that proves the same adapter contract against a **real**
coding-agent CLI, plus deterministic coverage that a malformed or missing
result file is a typed failure rather than a silent success.

---

## What this record proves directly (no external credentials required)

### 1. Malformed and missing result files fail closed, not silently

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
failure, not silently accepted."*

### 2. The builder / independent-reviewer / integration scenario is fully scripted against real primitives

`test_real_agent_builder_reviewer_integration_on_scratch_repo` in
`tests/test_real_agent_end_to_end.py` drives three `SubprocessExecutor`
launches (`BUILDER`, `REVIEWER`, `INTEGRATION`) against a disposable scratch
git repository created with real `git init`/`commit` calls, using three
distinct worker ids (`real-agent-builder`, `real-agent-reviewer`,
`real-agent-integrator`) so the reviewer is never the implementer. Each
execution's provider, runtime command, exit code, and result-file contents
are written to a durable JSON evidence file per role (see
`_record_evidence` in that test).

The worker identities and roles come only from the templates declared in
`examples/public_dogfood/real_agent_end_to_end_demo.yaml` plus the operator
environment variable `BUILD_COORDINATOR_REAL_AGENT_CLI` — there is no
per-run manual worktree or worker choice.

---

## What still requires an operator to execute live

This task runs inside a sandboxed, path-scoped execution (`examples/`,
`docs/`, `tests/` only) with no ability to spawn another authenticated,
billed coding-agent CLI process as a nested subprocess. The scenario test
above is therefore **opt-in**: it is skipped unless
`BUILD_COORDINATOR_REAL_AGENT_CLI` is set to a real, installed, authenticated
coding-agent CLI, matching the same pattern documented for the Claude Code
runtime in [CODEX_ACCEPTANCE.md](CODEX_ACCEPTANCE.md) ("Readiness on an
operator machine still requires `stagemesh agent setup` to complete a live
headless probe.").

To produce the live durable evidence this task's acceptance criteria call
for, an operator with a real coding-agent CLI installed should run:

```
BUILD_COORDINATOR_REAL_AGENT_CLI="<real cli invocation, e.g. 'claude -p --output-format text --dangerously-skip-permissions'>" \
  pytest tests/test_real_agent_end_to_end.py::test_real_agent_builder_reviewer_integration_on_scratch_repo -q
```

This produces `<tmp>/evidence/{builder,reviewer,integration}-evidence.json`,
each recording provider, runtime, exit code, and the result-file path/
contents for that execution, and asserts:

- the builder's `feature_sha` is a real commit on the task branch,
- the reviewer's worker id differs from the builder's and its
  `reviewed_feature_sha` matches the builder's commit,
- integration reports the same `feature_sha`/`reviewed_feature_sha` it
  received.

## Claims not made

- **Live run evidence attached here:** not claimed. This document describes
  the reproducible mechanism and the deterministic typed-failure coverage
  that ships with it; the live scratch-repo run must be executed by an
  operator holding real coding-agent credentials, per the command above.
- **Self-hosting proven:** not claimed; unrelated to this task.
