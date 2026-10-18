# Reliable execution, validation and delivery

How a bounded task gets from admission to a merged change without repeated checks, contradictory reviews or operator handholding,
and what each state of that path honestly means. Every rule below is deterministic and covered by regression tests that use fake
providers, temporary repositories and controlled clocks (`tests/test_gate_execution.py`, `test_gate_timeout_diagnosis.py`,
`test_validation_selection_reuse.py`, `test_contract_feasibility.py`, `test_review_disposition.py`, `test_step_progress.py`,
`test_honest_delivery.py`, and the failure-to-delivery path in `test_reliable_delivery_end_to_end.py`).

## Gate execution and timeout diagnosis

* A gate whose command starts with an explicit path (`.venv/Scripts/python.exe`, `../tools/run`) is resolved against the gate's
  working directory before launch, because Windows resolves a relative executable against the *parent's* directory. The path is
  normalised, never `resolve()`d, so a virtual environment keeps its identity (`sys.prefix`) whether it is a symlink or a launcher.
* An explicit executable that does not exist fails the gate (`failure_kind: environment`) naming the path that was looked for. It is
  never replaced by a same-named program on `PATH`. A bare name (`npm`, `python`) is looked up on `PATH` and the lookup is recorded.
* Validation evidence keeps the declared command, the argv actually executed, how the executable was resolved, the working
  directory, the duration and a bounded (600 character), redacted tail of each output stream. Gate processes run with
  `PYTHONUNBUFFERED=1` unless the gate sets it, so a hang prints what it had reached instead of an empty log.
* A timeout is explained only from what its output supports:

  | class | evidence | effect |
  | --- | --- | --- |
  | `service_unavailable` | output names an unreachable service (connection refused, role/database missing, ...) | stops at once as an operational blocker; no provider retry |
  | `setup_timeout` | collection finished, no test reported a result, and the output after it names fixture/session setup | stops at once; diagnosis required |
  | `collection_timeout` | still collecting/importing | stops at once; diagnosis required |
  | `test_execution_timeout` | tests were running (a logged service error only counts if it is among the last lines) | ordinary bounded remediation (a candidate defect is plausible) |
  | `timeout_unclassified` / `no_output_timeout` | nothing printed, or output that does not identify the phase (including "collected N items" with no further evidence) | cause *not established*; one bounded remediation attempt, an equivalent repeat stops |

  Equivalent timeouts compare equal across candidates. StageMesh never skips fixtures, bypasses a database requirement, or edits
  client gates; the recommendation tells the operator to run the gate directly and fix the environment or the gate's declared setup.

## Validation selection and evidence reuse

* The bound task/candidate baseline measures the change and feeds the scope check. When that baseline is demonstrably behind the
  integration ref, files that appear in the diff only because other work landed after it are reported
  (`selection.excluded_from_selection`) and kept out of *automatic* selection (browser end-to-end, nearby tests, risk class). The
  baseline itself is never replaced.
* A frozen gate that names its own comparison base (`--base origin/main`, a SHA, `A...B`) is reported in
  `selection.declared_gate_bases` and preserved.
* Required gates and discovered (exploratory) gates are listed separately in `check_roles`.
* Within one attempt, gates with the same command, directory, environment and limit run once. Across attempts a PASSED gate is reused
  only when the candidate tree, contract digest, command, working directory, environment, time limit, resolved executable, platform
  and the reuse policy version are all identical; `validation_reuse` lists what ran and what was reused from which evidence.
  Failed results are never reused, reuse is all-or-nothing (if any distinct gate must run, all run, because gates in one checkout may
  depend on each other's side effects), and hosted checks, independent review and integration never use local reuse.

## Contracts that cannot be satisfied

Before a provider runs, `allowed_files` is reconciled with discoverable mandatory requirements:

1. `.stagemesh/policy/required-changes.json` (project owned, authoritative):
   `{"version":1,"requirements":[{"id","kind","description","paths":["docs/design/**"],"applies_to":["src/**"],"waived":false}]}`
2. The contract's own objective, acceptance criteria, invariants and cited `spec_refs` ("... must update `docs/x.md`").
3. Unconditional "must/required ... update/add/record ... `path`" sentences in `AGENTS.md` and `CONTRIBUTING.md`, where the token is a
   plausible repository path (known extension, or a directory that exists) and the sentence has a documentation/record cue or the file
   exists. Conditional, negated and speculative (possible runtime output) sentences are advisory only. Documents are read only from
   inside the project.

A conflict refuses admission (`contract_policy_conflict`) with one message listing the requirements and three legitimate ways out: an
audited `rebind-contract` by the contract owner, a narrowly scoped prerequisite task through the existing planning flow, or an
owner-recorded exception (`"waived": true`). A prose-derived requirement that is a false positive is dismissed by listing its id or
source (for example `"ignore": ["AGENTS.md:12"]`) in the same file. Nothing is widened, relocated or created automatically. `contract-check` reports the
same conflicts.

## Independent review

Findings keep their stable identity (`finding_identity`), who raised them, the exact candidate, contract digest, baseline and
review evidence (`review.finding_recorded`), and every resolution (`review.finding_disposition`). A candidate is review-satisfied
only when an independent PASS exists *and* no blocking review finding on that candidate is open. A later reviewer is not asked
while one is open (reviewers are not retried until one passes); nothing integrates past it. Only
`stagemesh finding-disposition --task T --finding ID --disposition refuted_with_evidence|waived_by_owner --actor A --evidence REF
--rationale TEXT` resolves it, and a remediated candidate always needs its own validation and review. The one exception is the
existing no-op re-check (the implementer declined to change anything): the reviewer that raised a finding may withdraw it by passing
the unchanged candidate on that single re-check, which is recorded as a `withdrawn_by_reviewer` disposition citing the PASS
evidence. A different reviewer's PASS never counts.

## Retries and step caps

`MAX_STEPS` is resumable only after meaningful progress: a later lifecycle stage, completion, or a new candidate whose failure
differs from the previous one (or has none). More evidence rows or an equivalent failure on a new candidate are activity. Each
capped run is recorded (`task.step_cap`); two consecutive unproductive capped runs, counted across windows and restarts, end as one
`BLOCKED_RECOVERABLE_OPERATIONAL` blocker (`step_cap_no_progress`) that points at `stagemesh diagnose`. An operator fresh start
(`retry-task`, rebind, rebaseline) ends the streak. Resumed runs continue from the persisted stage: completed implementation is not
replayed and remediation budgets stay scoped to task and stage.

## What "done" means

`stagemesh` distinguishes: **in progress**, **integrated locally**, **staged** (ledger DONE on the wave staging ref, no PR),
**delivery pending** (publication deferred by the window deadline or a failure; work preserved and resumable), **published**,
**awaiting checks**, **merged** (exact reviewed head landed upstream) and **externally blocked**. Progress and stop output qualify
a ledger `DONE` accordingly. A GitHub issue is commented and closed only after the merge (or in local mode), never at ledger DONE.
PR identity and the exact head are recorded (`delivery.pr_published`); the landing guard records the merge outcome.

A publication permission denial (an explicit permission/authentication/403 refusal, not a network error) is recorded once (`delivery.externally_blocked`), preserves the work,
stops the rest of that window's publication attempts and is not retried until access is granted and
`stagemesh delivery-unblock --task T --actor A --reason R` is run. No alternative branch, credential or force is tried.

## Quiet follow-through

`follow_through` persists once-only notifications, follow-up registration/completion and blocker consolidation in the ledger.
A scheduled follow-up must check `follow_up_active` and stop scheduling itself after `complete_follow_up`; the completion notice
is sent only when `notify_once` returns true. The window report lists one cause once with all affected tasks.

## Runtime artifacts

Build the versioned wheel from the reviewed commit, keep the previous wheel as the verified rollback, and install only when every
StageMesh execution, claim and window is idle (see `project-local-runtime.md`). Verify installed files against the exact reviewed
wheel. If a prior repair is already in the shared runtime, reconcile instead of overwriting.
