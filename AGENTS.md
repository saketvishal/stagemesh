# AGENTS.md

This repository contains StageMesh, a provider-neutral coordinator for durable multi-agent software engineering.

## Core invariant

**Stages belong to StageMesh. Agents and execution runtimes are replaceable infrastructure.**

An agent may implement, validate, review, remediate, or integrate work only within the stage and permissions assigned by StageMesh. Agent assertions do not advance the lifecycle by themselves; recorded evidence does.

## Development rules

- Preserve the lifecycle: planning -> implementation -> validation -> review -> integration.
- Preserve exact-SHA review. Approval for one SHA must never authorize a different SHA.
- Preserve independent-review requirements. A builder must not silently become its own reviewer.
- Keep task state, worker state, provider state, and model/session state separate.
- Treat provider/runtime failures separately from implementation, validation, review, and integration failures.
- Preserve leases, checkpoints, durable events, and restart/idempotency behavior.
- Prefer deterministic routing and typed outcomes over model-selected control flow.
- Keep provider/model integrations replaceable. Do not hard-code a single model vendor into core lifecycle semantics.
- Never weaken git/worktree/integration safety to make an agent appear successful.
- Never add Caventra-specific, legal-domain, private-product, or other proprietary behavior to this public repository.

## Normal self-development workflow

StageMesh should normally develop StageMesh through its own GitHub-backed workflow:

```bash
stagemesh continue --task GH-<issue>
```

Direct edits are appropriate for narrowly scoped repository administration or when StageMesh itself is unable to execute safely. Do not bypass StageMesh lifecycle evidence merely to mark work complete.

## Validation

Use focused tests while iterating. Expand to contract/dependent tests when behavior crosses module boundaries. Run broad/full validation only when justified by the repository validation policy. Do not repeatedly run the entire suite after documentation-only changes.

## Integration

A task is not DONE merely because an agent says it is finished or a pull request exists. DONE requires the lifecycle evidence required by StageMesh, including integration/merged-main acceptance where configured.
