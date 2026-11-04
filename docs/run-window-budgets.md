# Run-window step and continuation budgets

`run-window` has two nested, bounded loops. They are deliberately reported separately:

- A **child step** is one staged task dispatch. A wave may dispatch the same task again from its recorded state, up to
  `--max-steps` child steps for that task.
- A **wave run** is one appearance of the task in a survivor wave. Reaching the child-step limit does not erase the task,
  its candidate, evidence, findings, or audit history.
- An **outer continuation** is a new wave run for a task that reached its child-step limit and made meaningful durable
  progress. It spends one unit of `--max-continuations`.

Consequently, a task that keeps making meaningful progress can receive at most
`(max-continuations + 1) * max-steps` child steps in one run window. Activity alone is not progress. If a full wave leaves
no new candidate, stage, evidence, finding, or other accepted progress state, StageMesh does not schedule an outer
continuation.

The JSON report records `runs`, `child_steps`, `step_limit`, and `step_budget_state` for each task. The text report shows
the same wave-run and child-step totals. Every continuation decision records the window ID, wave-run number, child-step
count, step limit, continuations already used, continuation limit, progress evidence, and decision in the audit ledger.
This makes `no_progress`, `internal_step_limit`, `outer_continuation_scheduled`, and
`continuation_limit_exhausted` distinguishable after the run.

## Restart behavior

A process restart never rewrites an old window budget. A later explicit `run-window` invocation receives a new window ID
and a new per-window budget. The task still resumes from the same durable StageMesh database: prior candidates, evidence,
findings, stages, and audit receipts remain present. A restart therefore supplies another bounded window; it does not
silently grant steps inside the old window or discard its exhaustion evidence.

Operators should inspect the prior report and `task-doctor` before starting another window. Repeated new windows can
intentionally provide more aggregate work, so persistent scheduling remains an explicit operator policy rather than an
implicit retry path.

## Acceptance boundary

The protected Windows and Linux CI delivery group runs `tests/test_autonomous_window.py`. Those tests independently cover:

- multiple staged child steps completing inside one wave without spending an outer continuation;
- exact child-step and wave-run ceilings when progress continues;
- no-progress stopping after one bounded wave;
- exact continuation audit correlation; and
- database close/reopen durability of candidate, evidence, and audit history.

These checks establish bounded scheduler semantics and durable receipts. They do not prove that a provider produced a
correct implementation; validation, independent review, and integration evidence remain separate requirements.
