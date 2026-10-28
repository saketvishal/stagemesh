# Operator UI contract: shared read-only activity API

Design only (parent objective #135, activity history objective #134, this slice #354). Nothing here is implemented: there is no
server, no UI, no new dependency and no schema change. It specifies how one desktop/web UI reads the activity projection that
already exists in `src/stagemesh/activity.py` (`query_activity`), and lists the decisions needed before building it.

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

A single route is enough for the first UI slice. Task drill-down is `GET /api/v1/activity?task_id=<id>`, which the projection
already supports. A health/version route is listed under Decisions rather than assumed.

### Query parameters

Parameters map one-to-one onto `ActivityQuery`. All are optional.

| Parameter | Type | Rule |
| --- | --- | --- |
| `state` | `open` or `closed` | Exact match; anything else is `invalid_query` |
| `task_id` | string | Non-empty, at most 200 characters |
| `stage` | stage value | Must be a `Stage` value |
| `provider` | string | Non-empty, at most 200 characters; selects executions only |
| `since` | RFC 3339 timestamp with offset | Inclusive; a value without an offset is `invalid_query` |
| `until` | RFC 3339 timestamp with offset | Exclusive; `since` must be earlier than `until` |
| `limit` | integer | 1 to 200, default 50 |
| `offset` | integer | 0 to 999 (must be below the scan cap of 1000) |

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

- `provider` returns executions only; task and audit rows are not provider-attributed.
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

## Known gaps against #134

The projection is the first slice, not the whole objective. A UI built on v1 cannot yet show: a project/repository filter,
"created/opened" or "completed" period semantics (tasks expose only `updated_at`), reopen/retry or source-sync transitions
(only existing audit event types), status counts, opened-versus-completed series, or source-issue state. These need projection
work, not UI workarounds; the UI must not infer them from current state.

## Decisions needed before implementing #135

1. Server home: a new `stagemesh` subcommand, or a route in an existing process. Needs the process-lifetime and locking story
   against the runner, since reads must not contend with or mutate the coordinator.
2. Desktop shell choice (Tauri or equivalent) and how it receives the launch credential.
3. Remote hosting: authentication scheme, TLS and who may read which project. Until decided, loopback only.
4. Live updates: keep polling, or add server-sent events. Either must stay read-only.
5. Snapshot cursor to remove boundary repeats and skips, versus accepting client de-duplication.
6. Multi-project scope: one server per store, or a server that aggregates stores (changes the auth and error model).
7. Projection additions required by #134 (see gaps), and whether they extend `ActivityEntry` before or after contract v1.
8. A health/version route for the desktop shell to detect an incompatible server.
9. Dependency policy: the project has no runtime dependencies today; the server should use the standard library, and the UI
   build toolchain must stay outside the Python package.
