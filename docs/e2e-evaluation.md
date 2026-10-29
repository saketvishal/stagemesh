# Configured evaluation and remediation

StageMesh runs explicitly configured `browser` and `api` validation services in
a disposable checkout of the candidate. It records candidate/contract/service
identity, validates configured artifact paths and checksums, and supplies bounded,
redacted failure details to remediation. Environment/startup failures remain
distinct from assertions. The coordinator's existing retry budget applies;
the evaluation runner does not install tools or start inferred services.

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
`prefix`, `workspace(s)`, `include-workspace-root`, `global`, `location`,
`userconfig`, and `globalconfig`. Use explicit service `cwd` instead of selecting
another package through these settings. Tokens after `--` are passed unchanged
to the script. Quoted/escaped local npm configuration keys are rejected rather
than partially interpreting npm's INI syntax; metadata symlinks cannot escape
the candidate checkout. These distinctions follow
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
