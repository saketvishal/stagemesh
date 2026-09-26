# Public Dogfood Acceptance

This directory defines the public dogfood acceptance suite for StageMesh. It is
designed to prove the release-critical coordinator behaviors without publishing
private task text, repository names, customer data, credentials, or proprietary
product details.

The suite is intentionally scenario-based. Each scenario maps to one or more
demo manifests in `examples/public_dogfood/` and records the evidence a release
operator should capture when running the demo against fake or local scripted
workers.

## Coverage

The suite covers:

- Parallel builder capacity and isolated claims.
- Independent review, result validation, and high-risk governance.
- Provider fallback through expired claims, checkpoints, and resume context.
- Controlled interruption and recovery.
- Cleanup and dry-run cleanup auditing.
- Global invocation across registered projects.
- Sanitized GitHub delivery dry runs.
- Public demos for single-agent, staged-agent, cross-provider recovery,
  high-risk governance, and multi-project execution.

## Running the Suite

Use the manifest as the source of truth:

```bash
python -m pytest tests/test_public_dogfood_acceptance.py
```

The pytest check validates that every required behavior has a public scenario
and that the manifest does not contain private names, machine paths, or secret
shaped values. Operators can then run the command outlines in
`acceptance-suite.yaml` against disposable repositories.

## Evidence Rules

Public evidence may include:

- Scenario id, task id, worker ids, provider labels, state transitions, and
  redacted result JSON.
- Dry-run GitHub payloads with placeholder owner and repository names.
- Logs showing fake or scripted worker commands.

Public evidence must not include:

- Secrets, tokens, cookies, SSH keys, or credential paths.
- Private repository names, customer names, product names, or internal issue
  bodies.
- Absolute paths from an operator machine.

