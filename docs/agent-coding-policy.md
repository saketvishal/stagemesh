# Provider-neutral agent coding policy

StageMesh injects one effective coding policy into every implementation prompt produced by the common provider adapter. Codex, Claude,
Grok and custom command providers receive the same policy ID, hash and negative constraints. A provider claim that it followed the
policy is not evidence: exact-SHA diff inspection and independent review remain authoritative.

## Precedence and provenance

1. StageMesh supplies safe built-in defaults.
2. A repository may track `.stagemesh/agent-policy.json`; StageMesh reads the version at the task's pinned baseline commit, not an
   untrusted candidate copy.
3. A frozen task contract may carry narrowly named `agent_policy_exceptions` and an `agent_policy_owner`.

The canonical effective policy includes its source, constraints, task exceptions and exception owner. Its SHA-256 hash is attached to
the provider prompt, process-start audit event, candidate inspection and validation evidence. Resume or provider fallback rebuilds the
same effective policy from the frozen contract and baseline. An invalid baseline policy blocks provider launch.

The default constraints prohibit unrequested abstractions, speculative code, filler comments, unsolicited documentation churn and
policy evasion. Exceptions are limited to the schema's named values; unknown exceptions or exceptions without an independent owner fail
contract admission.

## Independent checks

Deterministic policy-file tampering is a blocking violation and the candidate is quarantined unless the frozen task contract contains
an owner-approved policy-change exception. Diff signals that depend on intent—new helper/framework/wrapper abstractions, future-feature
markers, narration-only comments and documentation churn—are labeled for independent review rather than represented as perfect semantic
detection. The review requirement is recorded in the diff inspection.

Agent-policy files and enforcement modules are also protected by the repository quality policy and CODEOWNERS. Code-owner enforcement
still depends on the repository's branch protection settings; the file alone cannot prove that GitHub requires approval.

## Audit meanings

- `agent_policy.attached`: the effective policy was placed in the provider input; the provider has not necessarily started.
- `agent_policy.activated`: the provider process started with that policy prompt. This does not prove model comprehension or compliance.
- `agent_policy.refused`: policy resolution failed before provider invocation.
- `candidate.diff_inspected`: StageMesh independently checked the actual candidate and recorded policy findings.
- `candidate.diff_quarantined`: a blocking scope or policy violation was preserved and cannot be promoted.

Known limits are explicit. Name and comment heuristics can produce false positives or miss semantically equivalent behavior, so they
escalate to review. They do not replace language-specific static analysis, tests, security review, or human product judgment.
