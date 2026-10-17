# Provider readiness

Provider readiness is the operator view of whether StageMesh can safely route work to each configured provider. It combines provider
cooldowns, live acceptance evidence and the stage-specific commands configured for the provider.

## Cooldowns

Provider cooldowns record temporary provider-side failures such as quota, outage or authentication/capacity trouble. They are not task
defects. `stagemesh doctor` reports cooling providers in the operational readiness section, and `stagemesh capacity` shows the same
cooldown when explaining which providers are eligible for a stage or task.

A cooling provider is skipped until the cooldown deadline passes, but it does not block the rest of the pool. Other eligible providers
can still implement or review work, subject to the normal capability and independent-review rules.

To clear a cooldown after the provider has recovered:

```sh
stagemesh cooldown clear <provider>
```

Only clear a cooldown when the underlying provider issue has reset or been fixed; otherwise StageMesh may immediately record a new
cooldown from the next failed attempt.

## Live Acceptance States

Live acceptance is provider evidence from a real `provider-acceptance --live` run. `stagemesh doctor` and `stagemesh capacity` report one
of these states for each provider:

| State | Meaning |
| --- | --- |
| `missing` | No live acceptance result has been recorded for the provider. |
| `failed` | The latest recorded live acceptance result did not pass. StageMesh treats this as a real provider readiness problem. |
| `stale` | The latest passing result is older than the configured freshness window. The provider has proven live behavior before, but the proof is no longer fresh. |
| `passing` | The provider has a passing live acceptance result inside the freshness window. |
| `retry_due` | A temporary provider failure is older than its retry interval. A real window rechecks it before routing work; expiry alone is not a pass. |

The freshness window is controlled by `routing.live_acceptance_ttl_seconds`. The default is 7 days.

Temporary failures do not wait for that seven-day window. After the six-hour quota cooldown, or the fifteen-minute timeout/transient
failure cooldown, a real `run-window` automatically runs a bounded live check in a disposable repository and records the fresh result.
The default smoke budget is 180 seconds per stage, capped by the window's remaining time. Explicit timeout and
`STAGEMESH_PROVIDER_SMOKE_TIMEOUT_SECONDS` overrides still apply. An active cooldown takes precedence. Another refusal renews the wait;
authentication and other failures remain blocked. `doctor`, `capacity` and `run-window --plan` never call providers. Concurrent starters
do not duplicate the same provider check.

## Optional By Default

Live acceptance is optional during normal operation. By default, `missing` and `stale` states are informational: they tell operators what
evidence is absent or old without blocking release-unrelated work. A recorded `failed` live check is different; StageMesh skips that
provider until a later live acceptance run passes.

Release-mode commands can make live acceptance required:

- `stagemesh doctor --strict-live` fails readiness unless every provider has fresh passing live acceptance.
- `stagemesh run-window --strict-live` refuses to start unless providers have fresh passing live acceptance.
- `stagemesh queue-run --legacy-unsafe --require-live-acceptance` refuses providers without a passing live acceptance result, so release-mode routing only
  uses proven providers.

Use strict live checks before treating a provider pool as release-ready.

## One Provider, Stage Commands

A provider may use different commands for different stages while remaining one logical provider. For example, the same provider can use an
implementation command for `IMPLEMENT` and a read-only review command for `REVIEW` through `implement_command` and `review_command`.

StageMesh still judges independence by the provider name, so a provider that implemented a candidate cannot review that same candidate
under a different stage command. `stagemesh capacity`, `stagemesh config` and `stagemesh doctor` show which command applies to each stage
and whether it is the default command or a stage override. See the provider configuration section in the README for the full command
syntax.
