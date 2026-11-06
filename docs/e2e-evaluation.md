# Configured evaluation and remediation

StageMesh runs explicitly configured `browser` and `api` validation services in
a disposable checkout of the candidate. It records candidate/contract/service
identity, validates configured artifact paths and checksums, and supplies bounded,
redacted failure details to remediation. Environment/startup failures remain
distinct from assertions. The coordinator's existing retry budget applies;
the evaluation runner does not install tools or start inferred services.

## Declared coverage and independent evaluation

An explicit service can declare `requirements`, a candidate-relative
`result_manifest`, an `evaluator`, and `require_independent`. Browser requirements
bind a target, named state, and viewport; API requirements bind a target, method,
and expected status. The evaluation command receives only the policy fingerprint
and must write a fresh, bounded JSON manifest containing that exact fingerprint
and one PASS result for every declared requirement. Missing, duplicate, unknown,
failed, stale, or forged results fail closed. When independence is required, the
configured evaluator must differ from the provider that produced the candidate.

Services that need a local application may also declare both `startup_command`
and `health_command`, with a bounded `startup_timeout_seconds`. StageMesh starts
only that declared command inside the disposable candidate checkout, requires the
declared health command to pass, and accepts no evaluation verdict unless owned
process-tree cleanup is confirmed. It never infers or installs a service.

Operator and task-detail surfaces expose only allowlisted policy fingerprints,
evaluator identifiers, requirement IDs/statuses, lifecycle booleans, and stable
failure signatures. Targets, commands, raw output, and result-manifest contents
are not copied into those projections. Repeated identical failures are correlated
by signature and feed the existing repeated-failure diagnosis and remediation
budget, making the stop decision visible without exposing raw evidence.

## npm package identity

For an npm script, put its package directory in the service's `cwd`:

```json
{
  "validation_services": [{
    "name": "checkout-flow",
    "kind": "browser",
    "cwd": "frontend",
    "command": ["npm.cmd", "run", "evaluate", "--", "--project=chromium"],
    "timeout_seconds": 120
  }]
}
```

The fingerprint includes the selected `package.json`, including pre/post hooks
and other package metadata, project-local `.npmrc` files between that package
and the candidate root, and inherited `npm_config_*` environment values. Only
digests of the added configuration inputs are recorded. Script definitions and
command arguments retain the existing manifest format. Configured `env_keys`
continue to bind their environment values too. JSON formatting and Git line-ending
conversion do not change metadata identity.

Executable names `npm`, `npm.cmd`, `npm.exe` and `npm.bat`, including explicit
paths, are recognized. Script forms include `run`, `run-script`, `rum`, `urn`,
`test` (`t`/`tst`), `start`, `stop`, and `restart`. The named script must exist;
implicit defaults such as `npm start` without a declared start script cannot
produce accepted script evidence.

Before `--`, supported npm options are `--silent`, `--if-present`,
`--ignore-scripts`, and `--foreground-scripts`. Other options fail closed.
Package/workspace selection and dry-run modes are also refused when supplied by
inherited npm environment settings or project-local `.npmrc`. This includes
`workspace(s)`, `include-workspace-root`, `global`, `location`,
`userconfig`, and `globalconfig`. Use explicit service `cwd` instead of selecting
another package through these settings. Tokens after `--` are passed unchanged
to the script. Quoted/escaped local npm configuration keys are rejected rather
than partially interpreting npm's INI syntax; metadata symlinks cannot escape
the candidate checkout. Command-line `--prefix` is refused, while the environment
or local-config `prefix` setting is hashed and allowed: it controls npm's global
installation location, not the package selected for a local script. These distinctions follow
[npm's script and workspace semantics](https://docs.npmjs.com/cli/v10/commands/npm-run-script/).

A changed package hook or supported configuration input invalidates old passing
evidence. Configuration is compared with the exact candidate before evaluation
and rechecked inside that checkout around each evaluation, then against the
original project after evaluation. A mismatch yields an environment failure for
remediation, never accepted passing evaluation evidence. Previously captured npm
evidence must be regenerated under this fingerprint policy.

## Limits

This is candidate-bound local evidence, not a hermetic environment or a guarantee
that a remote service is unchanged. Global/user npm configuration, external tools,
installed dependency bytes and arbitrary command wrappers are not fully captured.
Operators must keep those inputs controlled; configured service declarations do
not confer authorization to install tools, enable schedules or call paid providers.

The focused regressions cover real candidate checkouts, artifact and failure
handling, and simulated npm evaluation outcomes. They do not claim a live browser
or remote-service acceptance run. Parent objective #327 and the hands-off
development acceptance gate require separate evidence.

The Caventra design-to-code qualification, including the exact supported/missing
boundary and a synthetic visual-fidelity experiment specification, is documented
in [UI fidelity orchestration qualification](ui-fidelity-qualification.md).
