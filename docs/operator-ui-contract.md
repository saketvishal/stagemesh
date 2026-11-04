# Operator UI contract: shared read-only activity API

Parent objectives #135 (operator UI) and #134 (activity history). The server in `src/stagemesh/api_server.py`
(`stagemesh serve`, #446) implements this contract over the activity projection in `src/stagemesh/activity.py`
(`query_activity`). The UI, desktop launcher and per-user Windows installer are implemented by #448, #449 and #451. The contract needs no
dependency and no schema change.

## Principles

- The UI reads only this contract. It never opens the coordinator database or imports store internals.
- Every endpoint in this version is read-only: `GET` only, no request body, no state change, no audit write on read.
- The contract is a thin projection of `ActivityQuery` and `ActivityPage`. It adds no fields the projection does not produce.
- Unknown parameters, malformed values, out-of-range pages and unauthenticated calls fail closed with an error; they are never
  ignored, coerced or answered with partial data.
- Operator actions (retry, stop, approve) are out of scope. They will be separate, audited command endpoints with their own
  contract; none are reachable through the endpoints below.

## Transport

HTTP/1.1 with JSON bodies, versioned under `/api/v1`. One contract serves both clients: the desktop shell loads the same UI and
talks to a loopback server it starts or attaches to; the browser talks to the same routes. Live updates are by polling in v1
(see Decisions); the response carries no streaming semantics.

| Method and path | Purpose |
| --- | --- |
| `GET /api/v1/activity` | One page of activity entries, newest first |
| `GET /api/v1/health` | Authenticated liveness and `version`, so a shell can detect an incompatible server |

Task drill-down is `GET /api/v1/activity?task_id=<id>`, which the projection already supports. `health` takes no
parameters and returns `{"status": "ok", "version": ...}` so a shell can detect an incompatible server.

### Query parameters

Parameters map one-to-one onto `ActivityQuery`. All are optional.

| Parameter | Type | Rule |
| --- | --- | --- |
| `state` | `open` or `closed` | Exact match; anything else is `invalid_query` |
| `task_id` | string | Non-empty, at most 200 characters |
| `stage` | stage value | One of `PLAN`, `IMPLEMENT`, `VALIDATE`, `REVIEW`, `INTEGRATE`, `DONE` (a `Stage` value) |
| `provider` | string | Non-empty, at most 200 characters; selects executions only |
| `since` | RFC 3339 timestamp | Inclusive; see "Timestamp form" |
| `until` | RFC 3339 timestamp | Exclusive; `since` must be earlier than `until` |
| `limit` | integer | 1 to 200, default 50 |
| `offset` | integer | 0 to 999 (must be below the scan cap of 1000) |

Timestamp form: exactly `YYYY-MM-DDTHH:MM:SS[.f]Z` or `YYYY-MM-DDTHH:MM:SS[.f]+HH:MM` / `-HH:MM`, where `.f` is 1 to 9
fraction digits. Digits are ASCII `0`-`9` only (Unicode digits such as Arabic-Indic are rejected, also in `limit` and `offset`). `T` and `Z` are uppercase, the extended (hyphen and colon) form is required, and so is an offset. Date-only
values, ISO 8601 basic format (`20260101T000000Z`), lowercase designators, a space in place of `T`, offsets without a colon and
leap seconds are `invalid_query`. A `+` offset must be sent as `%2B`; a literal `+` is a space under form decoding.

Query-string decoding is strict. The raw query must be percent-encoded ASCII. A `%` not followed by two hex digits, a decoded
value that is not valid UTF-8 (`%FF`), and control characters (`%00`) are `invalid_query`; invalid bytes are never replaced with
U+FFFD. Valid percent-encoded Unicode (`caf%C3%A9`) and a literal percent (`100%25`) decode normally. Empty segments, a segment
without `=`, unknown names and repeated names (compared after decoding) are rejected, and error messages never echo values.

Time zones: the server compares instants and returns UTC. The UI converts the operator's local day, week or custom range to
absolute offset-bearing `since`/`until` before calling, so "Today" is the operator's local midnight to midnight, not UTC's.

### Response

`200` with this body (a serialization of `ActivityPage`):

    {
      "contract": "stagemesh.activity/1",
      "entries": [ ...ActivityEntry... ],
      "total": 0,
      "limit": 50,
      "offset": 0,
      "next_offset": null,
      "truncated": false
    }

Each entry has `kind` (`task`, `execution` or `audit`), `id`, `at` (UTC, RFC 3339), `state` (`open` or `closed`) and these
nullable fields: `task_id`, `stage`, `status`, `provider`, `title`, `execution_kind`, `event_type`, `candidate_sha`,
`result_code`. Null means "not applicable or withheld", never "unknown value was replaced".

What the projection deliberately withholds, and the UI must not expect: free-form execution results, audit payloads, provider
output and secrets. `result_code` is limited to a fixed outcome allowlist (`capacity`, `cleanup_incomplete`,
`executor_aborted`, `external_workspace_mutation`, `fenced_identity_unknown`, `findings`, `no_implementation_change`,
`operator_stop`, `orphaned`, `provider_permission_denied`, `provider_timeout`, `provider_unavailable`, `recovered_stale`,
`tool_execution_failure`); anything else is null. The UI must render an unrecognized future code as a generic value, not fail.

### Filter interactions the UI must reflect

- `provider` returns executions only; task and audit rows are not provider-attributed. The filter matches the raw stored
  actor while the returned `provider` is sanitized, so a row whose `provider` is null (withheld) can still match a filter.
- `state=open` or any `stage` excludes audit rows (audit events are history only, always closed).
- `since`/`until` apply to `updated_at` for tasks, `started_at` for executions and `created_at` for audit events.

## Pagination

Offset pagination over a stable order: newest first, ties broken by kind (audit, execution, task) then `id`, so consecutive
pages never overlap for an unchanged store.

- Continue with `offset=next_offset` while it is non-null; `null` means the last page.
- The projection retains at most 1000 rows per source and 1000 combined. `truncated: true` means older rows were excluded and
  `total` may undercount; the UI must say so and offer a narrower `since`/`until`, not imply completeness.
- Because rows can be added between requests, a long scroll can repeat or skip a row at a page boundary. The UI de-duplicates
  by `(kind, id)`. A cursor/snapshot token is a Decision, not part of v1.

## Errors

Non-2xx responses use one body: `{"contract": "stagemesh.activity/1", "error": {"code": "...", "message": "..."}}`. Messages
never include query values, paths, store details or secrets.

| Status | `code` | Cause | UI behaviour |
| --- | --- | --- | --- |
| 400 | `invalid_query` | Any `ActivityQueryError`, unknown parameter, repeated parameter | Show the message; do not retry |
| 401 | `unauthenticated` | Missing or wrong credential | Re-authenticate; render no data |
| 403 | `forbidden` | Disallowed origin or host | Stop; render no data |
| 404 | `not_found` | Unknown route | Programming error; surface it |
| 405 | `method_not_allowed` | Any method other than `GET` | Programming error; surface it |
| 503 | `store_unavailable` | Store missing, locked or mid-migration | Show a retryable state, keep last good data marked stale |
| 500 | `internal` | Unexpected failure | Show a generic error; never display a partial page |

The client pauses automatic refresh after 400, 401 or 403 and shows the unchecked auto-refresh control.
Corrected filters or an explicit refresh can request data again; a successful request does not silently re-enable polling.
The operator may explicitly enable auto-refresh again after correction. Network errors and 503 remain retryable, retaining
the last good data marked stale and the existing auto-refresh choice.

Each state needs distinct UI: loading, empty (valid query, no rows), truncated, stale (last good data after a failed refresh,
visibly marked and timestamped) and error.

## Local access and authentication boundary

The projection exposes task titles, provider names, candidate SHAs and audit event types, so reads are authenticated.

- Local desktop: bind to the loopback interface only, on an ephemeral port chosen per launch. Require a random per-launch
  bearer credential (`Authorization: Bearer ...`) that the desktop shell receives from the process that started the server and
  never writes to disk, logs or the URL. Reject any request whose `Host` is not the exact loopback host and port (DNS
  rebinding) and any `Origin` other than the app's own; send no permissive CORS headers.
- Browser against the same local server: the same credential, handed to the page in the launch URL's fragment
  (`/#token=...`). Fragments are never sent to the server or written to access logs; the page reads it, keeps it in memory
  only and removes it from the address bar. The static shell itself carries no data and needs no credential.
- Remote hosting: not specified. Binding beyond loopback is refused until the Decisions below are made. It must not be enabled
  by a flag that merely changes the bind address.
- Credentials and the store path never appear in error bodies, logs or the response.

## Accessibility and operator workflows

Requirements for the later UI, so the contract is sufficient for them:

- Every view is keyboard-operable with visible focus; filters, pagination and drill-down need no pointer.
- State is never colour-only: `open`/`closed`, `truncated` and `stale` carry text or icon labels, and meet WCAG 2.2 AA contrast.
- Tables have real headers; loading, error and stale changes are announced through a polite live region.
- Timestamps are shown in the operator's zone with the UTC value available; ranges state both bounds.

Workflows the single route supports: see what is open (`state=open`); see what finished in a window (`state=closed` with
`since`/`until`); follow one task (`task_id`); see one provider's executions (`provider`); page back through history.

## Version 2: recorded periods, projects and status counts

`GET /api/v2/activity` and `GET /api/v2/health` use contract `stagemesh.activity/2`,
including every error envelope. They retain the same authentication, loopback,
strict query decoding, read-only database connection and pagination bounds.
The existing `/api/v1` routes and shipped dashboard keep their original parameters,
response fields and timestamp behavior. V1 rejects the two new query parameters;
it does not silently ignore them. No dashboard, CLI or launcher change is required.

V2 accepts every V1 parameter plus:

| Parameter | Type | Rule |
| --- | --- | --- |
| `project` | string | Exact stored `tasks.project`; non-empty, at most 200 characters |
| `time_basis` | `activity`, `opened`, `updated`, `completed` | Selects the timestamp used for `since`/`until`, ordering and entry `at`; default `activity` |

The time window remains inclusive at `since` and exclusive at `until`, comparing
absolute timezone-aware instants. The meaning of each mode is explicit:

| `time_basis` | Task rows | Execution rows | Audit rows |
| --- | --- | --- | --- |
| `activity` | `updated_at` | `started_at` | `created_at` (the original mixed feed) |
| `opened` | `created_at` | `started_at` | Excluded; a generic audit event is not a work opening |
| `updated` | `updated_at` | `updated_at` | `created_at` (recorded events) |
| `completed` | Excluded: there is no task completion column | `updated_at` for SUCCEEDED/FAILED only | Actual `stage.transition` events with a valid non-DONE `from`, `to=DONE` and a task identifier |

Execution completion means **recorded terminalization**, not proof of OS process
exit or successful task delivery. UNKNOWN executions remain visible in ordinary
status counts but never qualify as completed: a recovery fence can retain an
unknown live provider. Failed attempts remain FAILED, not successful tasks.

Task completion occurrences remain audit entries with their own audit `id`,
`task_id`, recorded `at`, `stage=DONE`, `status=DONE` and `state=closed`. A reopened
task can have multiple completion occurrences in different periods; none rewrites
the task's present state or invents a historical task row. A legacy DONE task with
no recorded transition has no completion timestamp. In completed mode, `stage=DONE`
and `state=closed` include these events; other stages or `state=open` exclude them.
The `provider` filter continues to select executions only.

Every V2 entry adds nullable `project`, taken from the associated task's **current
recorded attribution**, not historical project membership. Unattributed tasks,
global audits and audits whose task is missing remain null. No project is guessed
from an audit payload's project field, a repository URL, or the database path.
Filtering matches the stored value exactly; displayed project text is bounded and
credential-redacted like titles. This is a filter within one store, not cross-store
aggregation or an authorization boundary. Task/project/event/time predicates apply
before each source's retention cap, so unrelated rows do not crowd out matches.

V2 adds `time_basis` and `status_counts` to the page response. For example:

```json
{
  "contract": "stagemesh.activity/2",
  "time_basis": "completed",
  "entries": [],
  "total": 0,
  "limit": 50,
  "offset": 0,
  "next_offset": null,
  "truncated": false,
  "status_counts": {
    "task": {"OPEN": 0, "CLAIMED": 0, "BLOCKED": 0, "DONE": 0, "UNKNOWN": 0},
    "execution": {"RUNNING": 0, "SUCCEEDED": 0, "FAILED": 0, "UNKNOWN": 0},
    "audit": {"DONE": 0, "EVENT": 0}
  }
}
```

Counts cover **retained matching rows before pagination** and therefore do not
change with `limit`/`offset` for an unchanged store. They honor every filter.
Task rows, execution attempts and audit occurrences have separate groups; never
sum them as a count of tasks delivered. Audit DONE counts completion occurrences
in completed mode; EVENT counts other feed events. Unrecognized task/execution
statuses use the fixed UNKNOWN count bucket, not a new free-text key. If
`truncated=true`, both counts and `total` are lower bounds and the client must
say so. They are not complete all-time statistics. No history is synthesized.

## Version 3: linked-source state and recorded transitions

`GET /api/v3/activity` and `GET /api/v3/health` use contract
`stagemesh.activity/3`. V3 retains all V2 fields and parameters and adds:

| Parameter | Type | Rule |
| --- | --- | --- |
| `source` | string | Exact linked task-source kind; bounded identifier syntax |
| `source_status` | string | Exact current source status for task, execution and audit rows; exact transition destination for source-history rows |
| `include_source_history` | `true` or `false` | Opt in to durable source status-change occurrences; default `false` |

V3 entries add nullable `source`, `source_id`, `source_status` and
`previous_source_status`. Task, execution and audit entries report the linked
source's current cached status separately from the StageMesh task `status` and
`state`. A `source` entry represents one recorded `STATUS_CHANGED` occurrence;
its `source_status` is the transition destination and
`previous_source_status` is the origin. First observation and unchanged cache
refreshes do not create transitions. Its task `status` and `state`, when a task
is linked, are the task's current recorded values rather than historical values
at the transition. Malformed or unsafe event payloads are not projected.

Source-history rows are available only for the `activity` and `updated` time
bases because the store records the transition instant but does not infer an
opening or completion time. Their filters, ordering, retention, pagination and
time bounds match the other activity sources. `status_counts.source` uses the
fixed `OPEN`, `CLOSED` and `UNKNOWN` buckets and counts retained transition
occurrences, not source issues or delivered tasks.

The V1 and V2 routes remain compatibility adapters: they reject V3-only query
parameters and omit every V3-only entry field and count group. V3 introduced CLI
`--source`, `--source-status` and explicit `--source-history` options. Existing
calls without those options retain the previous feed shape and do not include
source transition occurrences.

## Version 4: task status and rework occurrences

`GET /api/v4/activity` and `GET /api/v4/health` use contract
`stagemesh.activity/4`. V4 retains all V3 behavior and adds:

| Parameter | Type | Rule |
| --- | --- | --- |
| `task_status` | `OPEN`, `CLAIMED`, `BLOCKED`, `DONE` | Exact current status for ordinary rows; exact transition destination for task-history rows |
| `task_event` | `status_changed` or `rework_required` | Exact typed task occurrence; requires `include_task_history=true` |
| `include_task_history` | `true` or `false` | Opt in to durable task status and rework occurrences; default `false` |

V4 adds `task_transition` entries plus nullable `previous_task_status`,
`current_task_status` and boolean `rework_required` fields. A status occurrence
records a real durable status change only; repeated block/unblock calls and an
OPEN-to-OPEN stage change do not invent occurrences. A rework occurrence exists
only for a recorded `task.remediation_queued` event with a valid candidate SHA
and failed stage. Provider prose, task titles and current stage never imply
rework.

For task-history entries, `status` is the historical transition destination (or
`REWORK_REQUIRED`), while `current_task_status` is the task's present recorded
status. `stage` is the recorded transition/failed stage. Source fields remain the
linked source's current recorded state. These values are deliberately separate:
source closure does not close a task, and a historical reopen does not say the
task is currently open.

Task history supports only the `activity` and `updated` time bases. It uses the
same inclusive `since`, exclusive `until`, retention and pagination rules as the
other occurrence feeds. `status_counts.task_transition` has fixed `OPEN`,
`CLAIMED`, `BLOCKED`, `DONE`, `REWORK_REQUIRED` and `UNKNOWN` buckets. Counts
are occurrences, not unique tasks. Malformed or stale typed payloads are omitted,
and missing pre-feature history is not reconstructed.

V1-V3 reject V4-only parameters and omit V4-only fields, entries and count group.
When V4 history is opted in, typed task events replace their generic audit rows
to avoid duplicates; earlier contracts retain their prior generic audit view.
The CLI exposes `--task-status`, `--task-event` and `--task-history` and uses the
V4 serializer for JSON.

V4 also supports typed retry occurrences with `include_retry_history` set to
true, an optional exact `retry_kind` of `automatic_provider`, `blocked_reentry`,
`remediation` or `operator`, and an optional exact `retry_state` of `QUEUED`,
`CLAIMED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `UNKNOWN` or `SUPERSEDED`. A retry
entry uses `kind=retry`, a fixed `event_type=task.retry_queued`, and the allowlisted
kind/state. Bounded positive `retry_attempt` and `retry_limit` are
present only when both are valid and the attempt does not exceed the limit.
`candidate_sha` is emitted only when the durable event carries a valid candidate
identifier. Current task state remains in `current_task_status`. Migration 7
records the exact retry-event -> claim -> first execution relationship at those
transactional boundaries; only that linked execution can change the retry outcome.
An initial execution is never relabeled by ordinal position. A new queued retry
marks an older unstarted retry `SUPERSEDED`, and releasing its claim before an
execution returns it to `QUEUED`. Execution kind, safe actor and allowlisted result
code come from the linked execution row; provider text is never interpreted.
`status_counts.retry` partitions kinds and `status_counts.retry_outcome` partitions
states. Generic audit rows for the same allowlisted events are replaced only when
retry history is explicitly enabled. The CLI equivalents are `--retry-history`,
`--retry-kind` and `--retry-state`.

## Version 5: typed validation, review and integration evidence

`GET /api/v5/activity` and `GET /api/v5/health` use contract
`stagemesh.activity/5`. V5 retains every V4 field and adds explicit opt-in
`include_evidence_history=true` through the `include_evidence_history` query
field, exact `evidence_kind` values `VALIDATION`, `REVIEW` and `INTEGRATION`,
and exact `evidence_status` values `PASSED`,
`FAILED`, `CAPACITY` and `UNKNOWN`. Kind/status filters require the opt-in and
select evidence occurrences only; they cannot be combined with task-event or
retry filters.

An evidence entry has `kind=evidence`, `event_type=evidence.recorded`, the
allowlisted kind/outcome, exact task identity, safe candidate identity and the
task/source's current recorded state. It is a closed historical occurrence, not
proof that the task or source is currently closed. Evidence payloads, findings,
commands, URLs, credentials and provider output are never selected or serialized.
Malformed candidates are omitted; unallowlisted kinds/statuses fail closed.
`status_counts.evidence` partitions kinds and `status_counts.evidence_outcome`
partitions outcomes. Evidence supports only `activity` and `updated` time bases.

V1-V4 reject V5-only parameters and omit V5-only fields/counts. The CLI options
are `--evidence-history`, `--evidence-kind` and `--evidence-status`; JSON uses the
V5 serializer. No migration is required because this is a bounded projection of
the existing durable evidence table.

## V4 recorded periods and series

`GET /api/v4/periods` and its shape-compatible V5 route are separate bounded
reports because an interval is not a
point event. It requires RFC 3339 `since` and `until`; accepts `bucket=day|week`,
an IANA `timezone` (default `UTC`), and exact `task_id`, `project` and `source`
filters plus active-interval `limit` (1-200) and `offset` (0-999). The matching
CLI is `stagemesh activity-periods --since ... --until ...` with `--bucket`,
`--timezone`, `--task`, `--activity-project`, `--source`, `--limit` and `--offset`.

The response contains `active_intervals` and `series`. Each interval carries the
recorded `active_from`, nullable `active_until`, current task status/stage and
bounded task/source attribution. An interval overlaps the requested half-open
window when its start is before `until` and its end is absent or after `since`.
BLOCKED and CLAIMED remain active work; only a recorded transition to DONE closes
an interval, and a recorded DONE-to-non-DONE transition opens a new one.
`total_intervals`, `limit`, `offset` and nullable `next_offset` make interval
pagination explicit. The series and task counts cover the complete bounded query,
not only the current interval page.

Each day/week series bucket has UTC `start`/`end` instants derived from local
calendar boundaries in the selected timezone, so 23/25-hour daylight-saving days
remain correct. `opened_occurrences` includes the recorded creation and every
recorded reopen; `completed_occurrences` includes every exact transition to DONE.
`opened_tasks` and `completed_tasks` are unique within that bucket, so a repeated
occurrence never silently inflates its unique-task count.

Only tasks with one `task.created` anchor, a continuous valid status chain and a
final recorded status matching the task row contribute. Earlier or inconsistent
history is counted in `unavailable_tasks`, never reconstructed. `considered_tasks`
is bounded to 200 after exact filters; `truncated=true` requires a narrower query.
The report is read-only and returns no titles, audit payloads or provider output.

## Operator status snapshot

`GET /api/v1/operator` uses its independent `stagemesh.operator/1` contract.
It takes no query parameters, requires the same loopback host and Bearer token,
and opens the project store in SQLite read-only/query-only mode for one request.
The response is a bounded observation, not a command surface or proof that a
provider, process, approval, or external service is currently available.

The response contains an `observed_at` timestamp, `ok` or `degraded` summary,
fixed scalar `metrics`, `bounded=true`, `detail_limit=100`, and these explicit
section arrays: `attention`, `pending_decisions`, `active_executions`, `workers`,
`recent_failures`, `efficiency`, and `provider_selections`. Each array uses a
fixed field allowlist. Active executions omit PIDs and executable paths. Worker
rows omit process identity and executable paths. Provider-selection rows include
only bounded provider names, recorded order, eligibility, and redacted reasons.
Failure and pending-decision reasons are credential-redacted and have URLs and
filesystem paths replaced before serialization. Raw audit/evidence payloads,
provider output, commands, environment values, arbitrary URLs, and external
evidence URLs are never serialized.

`recent_failures.scope` distinguishes a still-current failure from history.
The efficiency section is explicitly a bounded sample; provider usage is shown
only when providers recorded it, and billing cost remains `UNKNOWN`. Pending
approval coverage always retains an `UNKNOWN` row because absence from the
bounded journal never grants permission. The shipped dashboard exposes Activity
and Status views, keeps task links as an explicit retained-history drill-down,
and performs no writes or operator actions.

## Task detail snapshot

`GET /api/v1/task?task_id=...` uses `stagemesh.task-detail/1` and requires
exactly one non-empty, percent-encoded task identifier. It shares the loopback,
Bearer, GET-only and SQLite read-only/query-only boundary. Missing tasks return a
generic `task_not_found` error without echoing the identifier.

The response keeps current facts separate from history: `task`, `source`,
nullable durable `candidate`, recorded `dependencies`, typed
`pending_decisions`, a newest-first `timeline`, and evidence/retry subsets. The
timeline is capped at 200 retained rows and reports `truncated`; it never
reconstructs events before durable lifecycle recording. Validation, review and
integration entries expose only kind, allowlisted outcome, candidate identity
and recorded time. Retry entries preserve exact queued/claimed/running/terminal/
superseded state and never relabel initial work. Current dependency and source
status are ledger facts, not inferred liveness or completion.

No audit/evidence payload, finding, command, environment, executable path,
provider output or arbitrary URL is selected. Pending approval coverage retains
an `UNKNOWN` row. The dashboard task pane consumes only this contract; task links
from activity, intervals, failures and pending decisions share the same view,
with an explicit action to enter the separately labelled retained-history filter.

## Known gaps against #134

V4 adds durable task block/reopen/DONE and candidate-bound rework occurrences from
this release forward. Successful durable claims add `OPEN -> CLAIMED`, while explicit
release, confirmed-stale recovery and safe unknown-execution fencing add
`CLAIMED -> OPEN`; rejected/duplicate claims and unchanged releases add nothing.
The shipped loopback dashboard now requests V5 plus the independent read-only
operator snapshot and exposes project, time-basis,
source/task status and source/task/retry/evidence history filters. A task drill-down
opts into every typed retained history while labelling retention/truncation honestly.
The dashboard renders the recorded period route as an opened/completed series and
an independently paged active-interval table, with day/week bucket, IANA timezone,
This week and This month controls. It never rebuckets or reconstructs history in
the browser. Cross-store aggregation and a project-switcher remain outside this slice.
Earlier task/source changes were not recorded and cannot be reconstructed from
current state. The parent objective is not complete merely because this projection is.

## Architecture decisions

Resolved from the existing architecture (standard-library Python, SQLite in WAL mode, no runtime dependencies, one
project store per `.stagemesh` directory). Each is reversible except the remote-hosting item, which stays open.

1. **Server home: a separate `stagemesh serve` process.** It opens the database read-only per request, so it never
   contends with or mutates the coordinator; WAL lets readers run beside the runner. Embedding it in the runner would couple
   UI availability to run-window lifetime. Implemented (#446).
2. **Desktop shell: the same web UI in a chromeless browser app window** (Edge or Chrome `--app` on Windows, system browser
   fallback), launched by `stagemesh ui` (#449). Tauri or another native shell would add a Rust toolchain and signing for no
   functional gain over a loopback page; the contract is shell-agnostic, so one can be added later. The launcher passes the
   token in the URL fragment, never prints it, and the client immediately removes the fragment after reading it into memory.
3. **Remote hosting: OPEN, needs a human decision.** Authentication scheme, TLS and per-project authorization are security
   decisions with no precedent in the repository. Until made, the server binds loopback only and has no option to bind elsewhere.
4. **Live updates: polling in v1.** It needs no long-lived connections in a single-threaded server. Server-sent events can be
   added later as a new route without changing this one.
5. **Pagination: offset plus client de-duplication by `(kind, id)`.** The retained window is capped at 1000 rows, so a
   snapshot cursor adds state for little gain. `truncated` already tells the operator when to narrow the range.
6. **Multi-project: one server per project store.** A project switcher launches or selects a server per project through the
   existing project registry, so no server aggregates stores and the auth and error model stays per project.
7. **Projection growth:** #450 introduces `/api/v2` and `stagemesh.activity/2` for explicit period semantics,
   recorded project attribution and partitioned counts. `/api/v1` remains an unchanged compatibility adapter.
   The shipped dashboard adopts V5 explicitly. V3 adds linked-source fields and opt-in transition
   occurrences; V4 adds typed task status/rework/retry occurrences; V5 adds typed evidence outcomes.
   Every version preserves earlier response shapes.
   Migration 7 adds only the durable retry-to-execution correlation table; activity queries remain read-only.
8. **Health and version route:** `GET /api/v1/health`. Implemented (#446).
9. **Dependencies:** the server remains standard-library Python and the UI remains dependency-free HTML, CSS and JavaScript
   shipped as package data (#448), so no Node toolchain is needed. Windows runtimes install the standard `tzdata` package
   because CPython on Windows does not ship the IANA timezone database required by recorded period buckets; Unix platforms
   use their system database. A deliberately dependency-skipping offline wheel install may return `timezone is unavailable`
   until `tzdata` is installed in that runtime; it never substitutes UTC or invents bucket boundaries.
