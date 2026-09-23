# Validation and Review Model

## Overview

The Build Coordinator enforces a strict review model designed to prevent
conflicts of interest and ensure code quality before integration.

## Review policies

Tasks can have one of three review policies:

| Policy | Description |
|---|---|
| `SELF` | The implementing worker can also review their own work |
| `INDEPENDENT` | A different worker (different worker_id) must review |
| `TWO_REVIEWERS` | Two separate reviewers, both different from the implementer |

## How independent review works

1. **Implementation completes** — the builder transitions the task to `REVIEW_READY`
2. **Reviewer claims** — any eligible worker with `CODE_REVIEW` capability can claim
   the review, except the last implementer
3. **SHA capture** — the runner records the feature branch HEAD as `reviewed_feature_sha`
4. **Review executes** — the reviewer inspects the code and writes a structured result
5. **Verdict validation** — the coordinator validates the verdict and consistency:
   - `GREEN` + `ready_for_integration: true` + empty `required_remediation` → integration eligible
   - `REMEDIATION_REQUIRED` → always routes back to implementation
   - Contradictory results (e.g., `GREEN` but `ready_for_integration: false`) → fail closed
6. **SHA drift check** — if the feature branch HEAD changes after review, integration
   is refused and `REVIEWED_SHA_CHANGED` is escalated

## Remediation cycles

When a reviewer returns `REMEDIATION_REQUIRED`:
- The task returns to `READY` for re-implementation
- The original reviewer is not required for the next review
- Maximum remediation cycles is configurable (default: 2)
- After the limit is reached, `REMEDIATION_LIMIT_REACHED` is escalated to a human

## Structured results

Reviewers must produce structured JSON, not free-form text. The coordinator
validates:
- `verdict` is exactly `GREEN`, `GREEN_WITH_NOTES`, or `REMEDIATION_REQUIRED`
- `ready_for_integration` is consistent with `verdict`
- `required_remediation` is empty for integration-eligible verdicts
- `reviewed_feature_sha` matches the captured SHA

Free-form verdicts like "APPROVE", "PASS", "REJECT" are explicitly rejected.

## Human escalation

The coordinator never asks an LLM to decide whether a human gate should be
cleared. Human escalation types are:

```
SCOPE_EXPANSION_REQUIRED          — task grew beyond approved scope
ARCHITECTURE_DECISION_REQUIRED    — architectural choice needed
REMOTE_PUSH_APPROVAL_REQUIRED     — push not allowed by configuration
MERGE_CONFLICT                    — mechanical conflict, not resolved automatically
MIGRATION_SCOPE_VIOLATION         — unexpected schema migration
SECURITY_POLICY_BLOCK             — security policy triggered
EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED — worker not configured
REMEDIATION_LIMIT_REACHED         — too many remediation cycles
TEST_FAILURE_REQUIRES_JUDGMENT    — tests failing, human judgment needed
COORDINATOR_INVARIANT_FAILURE     — internal consistency violation
REVIEWED_SHA_CHANGED              — feature branch drifted after review
```

Each of these is durable: it survives restarts and cannot be cleared by automation.