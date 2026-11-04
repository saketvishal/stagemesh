# Specification governance

StageMesh specification governance is opt-in and read-only. It inventories the configured authority, reports structural failures and
review-only ambiguity, and can invoke an explicitly configured OpenSpec validator. It never creates, rewrites, syncs, applies, archives,
or migrates a project specification.

## Configure an adapter

Pin the adapter contract in `.stagemesh/config.json`. OpenSpec `layout-v1` discovers active owners under
`openspec/specs/*/spec.md`, historical owners under `openspec/changes/archive/*/specs/*/spec.md`, and reports non-archive change
directories as pending rather than authoritative.

```json
{
  "spec_governance": {
    "adapter": "openspec",
    "version": "layout-v1"
  }
}
```

Existing custom-SDD projects may instead export the declarative `custom-json` contract already supported by
`CustomSpecAdapter`:

```json
{
  "spec_governance": {
    "adapter": "custom-json",
    "version": "1",
    "contract_path": "docs/spec-authority.json"
  }
}
```

The export is data, not plugin code. Its paths must remain inside the project and its owner and requirement IDs must be unique.

## Audit

```text
stagemesh --project <project> spec-audit
stagemesh --project <project> spec-audit --json
```

The report is bound to the exact Git `HEAD` and includes adapter/version, active and historical owners, pending changes, confirmed
structural blockers, review findings, and the next action. Output is bounded. Exit codes are:

- `0`: no structural blocker was found; the report may still require human review;
- `1`: a confirmed structural or configured-validator failure blocks the gate;
- `2`: configuration, adapter, project, or execution input could not be audited safely.

The command does not create a StageMesh database or modify specifications. It is safe to call repeatedly. A CI job or external
scheduler may invoke it, but scheduling is deliberately opt-in; StageMesh does not create or alter a persistent schedule from this
configuration.

## Candidate behavioral evidence

The pre-implementation impact manifest may be produced before behavioral evidence exists. At exact-candidate validation, however, an
added, modified, removed, or superseded requirement must name a passing behavioral gate and the exact candidate revision:

```json
{
  "behavioral_evidence": [
    {
      "kind": "browser",
      "gate": "browser-e2e",
      "status": "passed",
      "candidate_revision": "<full candidate SHA>"
    }
  ]
}
```

The manifest does not make that claim true by itself. StageMesh checks that the named gate actually executed for the candidate, and the
ordinary validation result must pass. Stale revisions, missing gates, or self-asserted gates that did not execute block validation.

Use `owner_decision_required` to label a genuine unresolved product decision; it remains a review finding and StageMesh does not choose
an answer. A `waiver_request` must contain a non-empty actor and a reason of at most 500 characters. It is visible for review and never
erases malformed, stale, structural, validator, or behavioral-evidence failures.

## Optional pinned OpenSpec validation

Projects that already installed and reviewed OpenSpec may opt into a local command and exact version pin:

```json
{
  "spec_governance": {
    "adapter": "openspec",
    "version": "layout-v1",
    "openspec_validation": {
      "command": ["openspec"],
      "expected_version": "1.13.2",
      "timeout_seconds": 60
    }
  }
}
```

StageMesh first calls the configured prefix with `--version`. Only an exact semantic-version match proceeds. It then appends the fixed
read-only arguments `validate --all --strict --json --no-interactive`, runs without a shell, uses a secret-minimized environment, and
records bounded redacted stdout/stderr and the exit status. Commands in a spec or impact manifest are never executed.

Missing tools, version drift, timeout, nonzero exit, malformed/non-object JSON, and a declared out-of-sync/stale state fail closed.
A zero exit code cannot override StageMesh structural findings or stale candidate/manifest binding. StageMesh does not install or update
OpenSpec and does not infer that validation performed sync or archive work. OpenSpec documents the non-interactive validation form in
its [official CLI reference](https://github.com/Fission-AI/OpenSpec/blob/main/docs/cli.md#openspec-validate).

Configure only a reviewed executable. StageMesh removes credential-like environment variables, fixes the non-interactive arguments,
and does not use a shell, but it does not provide an operating-system sandbox around that executable. The executable therefore retains
the filesystem permissions of the StageMesh process; use ordinary workspace isolation and least-privilege OS permissions as well.

## Adoption and rollback

1. Preserve current SDDs, ADRs, decisions, and historical documents.
2. Add one declarative adapter configuration; do not rename or bulk-rewrite specifications as part of adoption.
3. Run `spec-audit --json` and review the exact authority inventory and unresolved references.
4. Resolve real authority conflicts in the owning project through its normal owner-approval process.
5. Add the same read-only command to CI only after the baseline report is understood.
6. If the adapter is unsuitable, remove the `spec_governance` section and its CI invocation. No project specification needs restoration
   because the audit never writes one.

Changing adapter or validator pins is a reviewed policy change. Rollback must not relabel an active requirement as historical or invent
approval; it only disables the StageMesh audit boundary.

## Failure behavior and limits

- Structural checks can prove missing IDs, duplicate declared IDs, unsafe paths, unresolved declared references, conflicting status
  declarations, malformed evidence, and validator failures.
- Exact normalized requirement text appearing under multiple active IDs is a suspected overlap for review, not proof of duplication.
- Differently worded semantic conflicts, product intent, founder approval, legal correctness, and implemented browser behavior require
  independent evidence or human review. StageMesh labels uncertainty and does not guess.
- Historical owners remain discoverable but never satisfy active dependencies.
- Pending OpenSpec changes are visible but are not canonical authority.
- A passing OpenSpec structural validator is not behavioral test evidence and does not replace exact-candidate validation or independent
  review.
