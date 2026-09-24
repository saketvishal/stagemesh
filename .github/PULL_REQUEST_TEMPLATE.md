## What changed

Describe the bounded change and why it belongs in StageMesh.

## Lifecycle / architecture impact

- Which StageMesh stage(s) or invariants are affected?
- Does this change task state, worker state, provider state, review authority, integration, or restart behavior?

## Validation evidence

List focused tests, dependent-contract tests, and any broader validation that was required.

## Safety checklist

- [ ] No private-product or domain-specific logic was introduced.
- [ ] Exact-SHA review semantics remain correct where applicable.
- [ ] Independent-review policy remains correct where applicable.
- [ ] Provider/runtime failures are not confused with implementation failures.
- [ ] Restart/idempotency behavior was considered.
- [ ] Documentation was updated when the public contract changed.
