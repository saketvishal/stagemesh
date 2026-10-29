# Reliable execution, validation and delivery

How a bounded task gets from admission to a merged change without repeated checks, contradictory reviews or operator handholding,
and what each state of that path honestly means. Every rule below is deterministic and covered by regression tests that use fake
providers, temporary repositories and controlled clocks (`tests/test_gate_execution.py`, `test_gate_timeout_diagnosis.py`,
`test_validation_selection_reuse.py`, `test_contract_feasibility.py`, `test_review_disposition.py`, `test_step_progress.py`,
`test_honest_delivery.py`, `test_delivery_identity.py`, `test_delivery_followup.py`, `test_efficiency.py`, and the failure-to-delivery path in `test_reliable_delivery_end_to_end.py`).

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
2. The contract's own objective, acceptance criteria and invariants ("... must update `docs/x.md`"). Cited `spec_refs` remain
   governing validation context, but path-changing prose discovered inside them is advisory because citing an authority does not mean
   the task must edit every repository path mentioned anywhere in that specification.
3. Unconditional "must/required ... update/add/record ... `path`" sentences in `AGENTS.md` and `CONTRIBUTING.md`, where the token is a
   plausible repository path (known extension, or a directory that exists) and the sentence has a documentation/record cue or the file
   exists. Conditional, negated and speculative (possible runtime output) sentences are advisory only. Documents are read only from
   inside the project.

A conflict refuses admission (`contract_policy_conflict`) with one message listing the requirements and three legitimate ways out: an
audited `rebind-contract` by the contract owner, a narrowly scoped prerequisite task through the existing planning flow, or an
owner-recorded exception (`"waived": true`). A prose-derived requirement that is a false positive is dismissed by listing its id or
source (for example `"ignore": ["AGENTS.md:12"]`) in the same file. Nothing is widened, relocated or created automatically. `contract-check` reports the
same conflicts.

GitHub dependency validation uses both the current source scan and the durable StageMesh ledger. A ready issue may depend on a closed
or otherwise filtered issue with a durable StageMesh task; it is not rejected as an unknown dependency merely because that prerequisite
is absent from the current open-issue response. A cached issue without a task does not establish a dependency or prove completion;
the child stays blocked as an unknown dependency. The same rule applies to objective roots and closed issues discovered in the current
scan without task rows. Forward references to tasks materialized by the scan remain supported. The scheduler still requires the
recorded prerequisite task to be complete.

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

## Follow-through that runs by itself

A window that ends with authorized delivery still pending hands it to **one** follow-up, armed only when the invocation asked for it
(`run-window --follow-up`, with `--follow-up-interval` default 10m and `--follow-up-ttl` default 24h). The runtime path is:

1. `run_window` ends -> `register_pending_follow_ups` (tasks left `DELIVERY_PENDING`, `PUBLISHED` or `AWAITING_CHECKS`).
2. Each one is registered once under a key derived from **repository + task + candidate**, together with its exact identity
   (repository, task, candidate, PR URL, base), its explicit authorization (`publish` if the window may open PRs, `merge` only if
   automatic merging was requested, with the merge policy and required checks), the authorizing invocation and the narrowed
   `run-window` command that resumes it. Passing checks or a finished task never add a `merge` scope.
3. One schedule is created through the single supported mechanism (`followup_scheduler`: Windows Task Scheduler via `schtasks`,
   launching a small script in `.stagemesh/followups/`). Where no mechanism exists the report says so and prints the one command to run.
4. Each wakeup runs `stagemesh follow-up --key K`. It takes a per-follow-up OS file lock (a simultaneous wakeup does nothing),
   re-validates the identity, then runs the same narrowed `run-window` (no implementation replay, exact-head review and the
   landing guard unchanged) and decides from the **ledger**, not the exit code:

   | situation | result |
   | --- | --- |
   | PR merged at the exact reviewed head | complete; one completion notice |
   | hosted checks pending | keep waiting; one notice per distinct state, not per wakeup |
   | publication/merge permission denied (explicit 401/403/permission text) | recorded once as `EXTERNALLY_BLOCKED`, follow-up ends, no other branch/credential/force/route is tried; `delivery-unblock` plus a new `--follow-up` window starts a fresh generation |
   | transient failure | backoff (retry registry), at most 5, then one blocker |
   | ttl reached / check budget used | one blocker with the exact re-arm command |
   | candidate changed, PR head is not the reviewed candidate, PR closed, other repository | stale: one blocker, nothing is acted on |
   | published but `merge` was not authorized | ends: a person merges |

5. Completion, cancellation (`follow-up --cancel`) and expiry **remove the OS schedule through the adapter and verify it is gone**
   (`schtasks /Delete`, then `/Query`). If removal fails the failure is recorded once and the next wakeup only retries the cleanup.
   Finalization is idempotent, so a crash between "completed" and "removed" or "announced" is repaired by the next wakeup (a
   notice is announced at most once).

`follow_through` holds the persisted facts (`INSERT ... WHERE NOT EXISTS`, so racing processes produce one row and one winner).
`stagemesh follow-up --list` shows active follow-ups. Cancelling or re-arming is an operator decision and refuses to run inside a provider process.

## Finding dispositions are attributable

A disposition records the actor, the exact candidate, the finding's severity and originating reviewer, what each evidence reference
resolved to in the ledger, and the implementers it was checked against. It is refused when the actor is (or is named like) an
implementer of the task or an automation label, or when requested from inside a provider process (`STAGEMESH_PROVIDER_PROCESS`, set
for every provider subprocess). `refuted_with_evidence` additionally needs `--evidence review-evidence:<id>` resolving to a **passing,
independent review of the same candidate recorded after the finding** by a reviewer that neither implemented nor raised it (machine
validation alone cannot rebut a reviewer). `waived_by_owner` is an explicit risk decision and is stored as `owner_attestation`, never
as independent evidence. The decision is written before the finding is closed, so an interrupted disposition is completed, not
duplicated; adverse findings and dispositions are append-only.


## Distinct normal and adverse review

Staged execution with `require_independent_review` earns a normal review checkpoint, then a separate adverse review
on a later coordinator tick. The task stays in REVIEW until both pass the prerequisite policy; the existing driver
checks stop requests and task budgets between the two ticks. Each role dispatch and incomplete-answer retry also
checks the shared configured task and originating-window budgets. Direct `Reviewer.review()` calls do not provide
the driver's stop-request boundary. Normal findings require remediation. Adverse findings at configured blocking severities
require remediation; actionable advisory findings stay in evidence. Missing, malformed or incomplete verdicts never
become advisory passes. Custom severity names remain supported by the policy.

Both roles must execute under distinct providers and configured command identities, independently of the implementer.
Selection uses only the existing authorized provider pool, excludes actual fallback actors and their command aliases,
and holds when another qualified reviewer is unavailable. This does not register or authorize another provider.
Passed role evidence references separate successful REVIEW execution records and the exact candidate, contract and
policy digest. A saved normal checkpoint can resume after reopening the database; it cannot stand in for adverse review.

New real implementation candidates carry an audit receipt for the configured command and same-actor aliases frozen
before prompt expansion, bound to their successful implementation execution. Review startup reports the actual winning
provider's frozen command identity. Receipts contain provider names and command fingerprints, never raw command argv
or prompts. Current configuration cannot reconstruct an older candidate's command provenance. Missing or conflicting
historic receipts hold qualification; old single-role review evidence does not satisfy the distinct-role gate.
This checks the configured provider/command independence policy, not universal semantic equivalence of different tools.
No existing runtime task, frozen candidate, contract or provider authorization is rewritten by this source change.

Each review fallback invocation is recorded and budget-checked individually. A provider that returns an incomplete answer or infrastructure failure spends an attempt before another configured reviewer can be tried; the outer review does not count the final attempt twice. Usage not reported by a provider remains unknown.



Successful owned rebase, reattribution and reconstruction preserve the implementation identity through a typed
candidate-derivation receipt. It points to the original successful execution and its original SHA, without inventing
another provider execution. Missing, conflicting, cyclic or overlong provenance stays unavailable. Each replacement
SHA must earn its own validation evidence and both review roles. The derivation itself copies no validation/review
evidence; normal validation policy may reuse eligible machine checks, while both reviews execute for the new SHA.


## Effort accounting and budgets

`provider.call` (task, candidate, provider, purpose `implementation`/`remediation`/`review`/`diagnosis`, outcome, normalized failure
class, duration, provider-reported usage) and `stage.transition` events feed `stagemesh efficiency` together with the existing
executions and `validation_reuse` evidence. Usage the provider did not report is `null`/unknown and is never summed as zero; keys are
`input`/`output`/`cached_input` because the audit redactor masks any key containing the word for a credential. No prompts, provider
output or client content is stored. Budgets (`budgets` in `.stagemesh/config.json`, defaults: 5 consecutive equivalent failures, above the bounded hands-off provider-exhaustion recovery, 30
provider calls per task; optional per-window calls and reported-usage caps) come from the ledger, so they survive new windows and
restarts, and are checked between stages and waves, with additional checks before staged pooled review attempts and fallbacks; reaching one is a single typed `BLOCKED_RECOVERABLE_OPERATIONAL` blocker
(`budget_exhausted`) and a fresh start by the operator (`retry-task`) resets the task's counters. Budgets never skip validation or review.

## Validation gate cleanup

Validation-gate cleanup is confirmed separately from the root process exiting. Failed tree enumeration, identity,
signal or reap/drain checks stop the current run with cleanup incomplete. The validator retains its claimless
RUNNING execution and unknown cleanup fence; it records the gate identity and retains both the candidate checkout
and private gate TEMP directory. It emits no failed/passed validation verdict or remediation attempt from uncertain
cleanup. A new run cannot recover this fence merely because the old parent exited. Confirmed ordinary failure and
timeout still clean their temporary directories. This covers the observed gate tree and task ownership; it does not
establish universal detached-child cleanup or a durable cross-project
validation-slot fence after a parent crash.

If cleanup ownership cannot be durably stored, the current run still propagates cleanup-incomplete control flow; restart protection is not qualified until that fence is successfully persisted. A detail-audit failure after retention does not erase the saved UNKNOWN fence or turn cleanup uncertainty into an ordinary failure verdict.

## Active validation-gate control

An active gate polls the existing scoped stop, cancellation and window-deadline
controls while communicating, and checks them again before accepting output.
A terminate-running stop is sticky for that run even if a resume follows. Pause
and drain-only stop let the active gate finish; no next stage is authorized by
that completion. The gate's own monotonic timeout budget is not reset by polls.

Confirmed cancellation reaps the observed owned tree and drains its pipes, then
propagates operator-stop control without validation verdict, findings or a
remediation attempt. The validator terminalizes its execution with operator_stop
(or stop_monitor_failed when monitoring failed), leaving the candidate available
for a new run after resume. If cleanup cannot be confirmed, the existing UNKNOWN
ownership fence and candidate/TEMP retention still block restart. A stop observed
after ordinary root/pipe completion suppresses the verdict without a new signal;
this does not add a universal detached-child guarantee. Storage unavailability
still propagates the original control event; durable completion is not claimed.

## Existing stage ownership at dispatch

The coordinator rechecks persisted RUNNING executions after known-dead owner recovery and before advancing a task.
An existing live or unknown execution, including a claimless cleanup fence, prevents another stage from starting.
This also protects legacy `continue --once` and ownership published after parallel batch selection. A single-pass
attempt with no work reports `REFUSED:active_execution`; unrelated tasks can still advance. Existing ownership,
candidate evidence and stage remain unchanged. This recheck alone does not reserve an otherwise empty stage
against simultaneous fresh callers. The [Coordinator dispatch guard](#coordinator-dispatch-before-execution-publication)
now serializes cooperating Coordinator entry points; arbitrary direct stage API calls remain outside its guarantee.

## Runtime command failure evidence

The configured implementation runtime records the same typed, bounded, redacted
`task.tool_failure_evidence` as the bare subprocess executor after communication
and workspace ownership checks complete. Records identify the actual execution,
owned working directory and configured stage command before prompt expansion.
Expanded prompt-bearing arguments are not used as the command identity. Split or joined credential values and their echoed values are redacted before truncation. Ambiguous display-only string commands (malformed quotes or backslashes) conservatively hide identity/output; execution callers supply explicit argv. Provider
quota, authentication, availability and transient-provider classifications retain
their existing semantics and do not become tool-failure evidence. Unknown cleanup
or workspace mutation does not publish an ordinary tool-failure record.

Recovery recommendations remain unexecuted: no automatic replay, setup or install
is added. A repeated signature after reopening the store stops with
`execution_status=NOT_CONFIRMED` and an empty actual-attempt history. This does not
satisfy the nested-tool recovery executor or live unattended-delivery acceptance.

## Validation ownership after parent loss

A RUNNING validation row is committed before any gate can launch. It remains
child-tree authority until actual validation completion or an explicit inspected
release; a dead parent, absent child PID, DONE task, or old row without identity
is not proof that no gate survives. Native Windows parent-only kill tests cover
both the prelaunch window and an active surviving gate and refuse a new runner.

Known-live parent identity remains visible as LIVE. Dead or unknown validation
parents are UNKNOWN for automatic recovery. Coordinator, single-pass/parallel
admission and autonomy recovery hold the row, claims and workspace; even a policy
that normally fences unknown implementations cannot replace validation ownership.
Storage mutation backstops reject automatic orphan/fence operations. The operator
status includes UNCONFIRMED_VALIDATION_TREE rather than treating parent death as
successful cleanup. Global health, operator text/JSON and the dashboard expose
parent liveness separately from effective recovery ownership. Non-live validation
trees count as unresolved and require review, rather than stale automatically
recoverable executions. This read-only count does not change persisted UNKNOWN
execution status totals; a live validator does not itself raise a cleanup alert.
Admission keeps its existing policy: a provably dead parent remains a global
stale-parent hazard, while unknown parent identity alone does not block unrelated
work. The new inspection warning cannot itself authorize or prevent a stage;
existing task ownership and global hazard gates remain authoritative. Handoff
exports retain the same parent/ownership distinction and advise inspecting a
validation tree before a selected release, never ordinary automatic recovery.

The deliberate safety tradeoff also holds legacy, prelaunch and static-only
validation crashes: durable data cannot prove a child never started. After
inspection establishes that no child remains, per-execution release-unknown with
a nonempty reason is available; LIVE parents remain protected. The selected
validation release and full operator audit commit atomically. Confirmed normal
success/failure/cancellation still closes the row and permits an ordinary new run.
No automatic child-tree reconstruction/reaping, universal detached-child cleanup
or durable cross-project slot recovery is claimed.

The configured implementation-only quarantine policy remains available only at
IMPLEMENT when every existing owner is an implementation. Ownership is read again
after that policy runs: a boolean response alone, or a newly published validation
owner, cannot authorize another stage. That policy alone is not a fresh-stage reservation;
the [Coordinator dispatch guard](#coordinator-dispatch-before-execution-publication) supplies serialized admission
for cooperating Coordinator entry points.

## Runtime artifacts

Build the versioned wheel from the reviewed commit, keep the previous wheel as the verified rollback, and install only when every
StageMesh execution, claim and window is idle (see `project-local-runtime.md`). Verify installed files against the exact reviewed
wheel. If a prior repair is already in the shared runtime, reconcile instead of overwriting.

Source synchronization publishes task eligibility only after dependency and retirement updates finish. A task shown as `source synchronization incomplete` cannot be admitted or claimed, including by a worker holding an earlier scheduling decision. An interrupted import keeps that task unavailable until an authoritative retry reconciles it; an empty scan or an unrelated source does not release it. Source writers for the same database serialize their publication phases. This protects partial imports; it does not establish freshness of remote snapshots fetched before the synchronization lock.


Tool-failure recovery records are recommendations. The subprocess failure recorder does not execute the proposed
retry or setup command, so its `attempted` list stays empty and `execution_status` is `NOT_CONFIRMED`. A matching
failure in the task-scoped audit history stops and reports the latest 20 `previous_recommendations` and their total count for operator verification;
recording an unexecuted retry never authorizes progression to setup. Actual configured recovery execution remains
separate unfinished work. No setup command is derived from provider output or run by this accounting path.

Recovery history is streamed for the exact task and event type before signatures are compared. Unrelated events or other signatures cannot evict an earlier stop decision through a global recent-events limit. The full audit remains authoritative; the recovery summary is bounded.

When an execution-aware caller supplies attempted actions for an exact failure signature, the planner allows only one recovery action in total. A prior retry, setup or unknown action stops with that history and the exact operator action; configuring setup cannot chain another action after a retry. The initial retry/configured-setup paths remain recommendations, not execution receipts.


## Coordinator dispatch before execution publication

For persistent stores, Coordinator stages acquire a nonblocking native lock for the connected SQLite `main` database filename and task ID before re-reading task state, current Scheduler eligibility, ownership and contract
gates. The lock spans the stage and its state publication, so two processes or
threads cannot both pass an empty-owner check and dispatch the same stage.
Different database/task keys remain independent. A busy or unavailable lock
returns an explicit dispatch refusal without a fabricated failed execution,
evidence, claim, or remediation attempt.

This reuses the existing integration lock mechanism with a distinct hashed
resource per task; it adds no scheduler, database migration or provider fallback.
Lock files contain no authoritative PID and remain stable anchors across runs.
Kernel release after parent death does not authorize child replacement: durable
RUNNING ownership still applies, including the validation inspection fence.
This guards Coordinator dispatch entry points, not arbitrary direct stage API
calls or every external database writer. It does not prove universal child-tree
cleanup or a durable cross-project validation slot after a crash.

Stale selections are rechecked under the acquired lock, including DONE/BLOCKED,
dependency and source-publication eligibility. This is the dispatch admission
point; it is not a transaction over arbitrary external writers during an already
admitted stage. Pre-entry filesystem errors refuse dispatch; errors raised by a
stage after entry retain their original handling. Private `:memory:` databases
keep connection-local behavior and do not create invalid native lock filenames;
they do not establish durable, cross-process ownership.

Public continue/drive results distinguish `stage_dispatch_unavailable` and
`stage_dispatch_ineligible` from `active_execution`. A lock failure or stale
selection is not evidence that an execution exists. Mixed refusals retain each
task reason without claiming that every refusal represents process ownership.

The lock namespace comes from the open SQLite connection's `main` filename.
Changing the working directory after opening a relative Store path cannot select
another lock. Only a verified empty `main` filename takes memory behavior;
missing, malformed or failed identity lookup refuses dispatch, never silently
becomes an unlocked memory operation.


## Research subprocess ownership across launch loss

The optional read-only research role snapshots the workspace before launch and
again after confirmed process completion. The digest binds HEAD, index/worktree
status, changed and untracked content, and relevant ignored files such as local
configuration or runtime inputs. Tracked paths hidden by Git's assume-unchanged
or skip-worktree index flags are hashed explicitly. Dependency, cache, and generated-output trees
(`.venv`, `venv`, `.tox`, `node_modules`, cache directories, and `dist`) are
explicitly excluded; ignored `.stagemesh` authority files are not. Any covered
change blocks implementation as `research_mutated_source`.

The optional research role remains a REVIEW execution. New executions commit
explicit process_tree_scope=research/v1 and the initiating parent identity in
the same durable RUNNING row before Popen. Publishing the child identity does
not remove that authority. Cleanup-incomplete retention preserves the scope;
only confirmed owner completion or an explicitly inspected release ends it.
Actor names and provider output are not ownership authority.

Automatic orphan recovery, claim expiry/replacement, autonomy fencing and
workspace takeover refuse this held task. A missing or dead recorded root
does not prove that its descendants stopped. Known-live roots stay LIVE;
other scoped roots remain UNKNOWN for recovery. Status, health, dashboard,
queue and handoff expose the unconfirmed research/process-tree hold. Unknown
non-null scope versions also hold rather than guessing their cleanup contract.
As before, unknown identity alone is task-local; a provably dead root remains
a global stale-root admission hazard.

After inspecting surviving processes and retained work, the existing
recover-stale --release-unknown action requires the exact task/execution and
a nonempty inspection reason. It refuses live roots and binds its inspected
identity snapshot to the atomic row/audit update, refusing a parent-to-child
identity change during inspection. Releasing a claimless research row does
not release an unrelated implementation claim: that claim retains its existing
provenance/expiry rules. No immediate replacement is implied before that
independent claim can safely be acquired.

Migration 5 adds the nullable scope without rewriting legacy execution kinds,
actors or results. Concurrent v4 upgrades and an interrupted column/version
publication are serialized and idempotent. NULL legacy REVIEW rows retain
their prior behavior: pre-upgrade researchers cannot be reliably reconstructed
from configurable actor names or missing process data. That historical ambiguity
is UNQUALIFIED; upgrades must not assume an older anonymous row proves no child
survives. This change does not discover or reap arbitrary surviving processes,
activate schedules, promote a runtime, or establish live unattended acceptance.
