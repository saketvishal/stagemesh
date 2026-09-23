# Staged Multi-Agent Example

Two separate agents: one for building (implementation + remediation) and one
for independent code review. This is the recommended setup for enforcing
review separation.

The coordinator enforces that the worker who performed the implementation
cannot claim the review. The reviewer must be a different `worker_id`.

## Setup

Use the provider-neutral YAML format (recommended):

```yaml
workers:
  - id: builder-a
    role: BUILDER
    # ...
  - id: reviewer-1
    role: REVIEWER
    # ...
```

The `provider` field is an operator label. You can run both workers on
the same provider (e.g., both using Codex) or on different providers.
The coordinator does not require cross-provider review — that is a policy
decision for your team.

## Independent review enforcement

When a task has `review_policy: INDEPENDENT`:
- The last worker who performed the implementation is ineligible to review
- If the reviewer disappears (expired lease), the task returns to REVIEW_READY
  and any other eligible reviewer can claim it
- The reviewer captures the feature branch SHA at claim time; if the remote
  branch head changes before integration, the review is invalidated