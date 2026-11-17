# SDD-GAP-002: accountable planning approval identity and role separation

Status: PROPOSED SPECIFICATION ONLY

Specification version: `sdd-gap-002/1`

Parent governance record: issue 811 and `docs/sdd-governance-audit-811.md`

Owning issue: 820

## Authority and activation

This document specifies the bounded remediation for `SDD-GAP-002`. It implements no runtime behavior and does not activate a governed profile. Its authority is limited to the following founder-approved rows in the parent governance record:

- `SDD-AUTH-003`: implementation dispatch requires an approved planning spec bound to the contract digest and baseline SHA;
- `SDD-AUTH-004`: a required challenge is independent and accepted before approval;
- `SDD-AUTH-005`: the approver is named and differs from planner, challenger, and implementer;
- `SDD-AUTH-010`: review is independent from implementation; and
- `SDD-AUTH-014`: amendments are immutable new versions and stale identities block.

This document becomes an implementation authority only after all of the following occur:

1. this file is the only substantive file in its specification pull request;
2. an independent reviewer who did not author this file reviews the exact pull-request head;
3. the founder approves that exact head;
4. required hosted checks pass; and
5. the specification pull request merges.

Any implementation issue must pin both the exact merged specification commit and the SHA-256 digest of this file. The implementation contract must exclude this file and all other specification files. The implementer may not edit, amend, reinterpret, or approve this specification.

## Problem

The current planning gate accepts a local approval JSON object when `approved` is true and the planning-spec digest matches. When a challenge is required, the challenge digest must also match. The approval does not identify an accountable actor, bind the current approval authority, or prove separation from the planner, challenger, and implementation actor. A copied or hand-edited JSON object can therefore satisfy more of the gate than the governance record permits.

## Required behavior

When accountable approval is configured, StageMesh must:

1. accept only a version-2 approval artifact produced by an explicit operator approval action;
2. require a matching durable approval receipt recorded by StageMesh outside every provider execution;
3. derive the approver identity from the operator-controlled approval-authority configuration, never from a provider response or a free-form command argument;
4. bind the approval to the task, planning spec, accepted challenge when required, task contract, baseline commit, and approval-authority policy;
5. compare the configured approver with the executed planner, executed challenger, selected implementer, and final reviewer identities;
6. fail closed when any required identity or binding is absent, malformed, stale, or conflicting;
7. re-evaluate the same bindings before initial implementation dispatch, resumed or replacement implementation, review dispatch, integration, and promotion; and
8. preserve current behavior for projects that do not explicitly configure accountable approval.

An approval is permission to implement one frozen plan at one frozen baseline. It is not code review, objective acceptance, permission to merge, or founder approval of any other specification.

## Canonical identities

An accountable identity is a lowercase ASCII identifier matching:

```text
[a-z0-9][a-z0-9._:@/-]{0,99}
```

The configuration and approval artifact must already contain canonical identifiers. StageMesh must not silently rewrite invalid identifiers. Comparisons use exact canonical strings.

Planner, challenger, implementer, and reviewer identities are taken from their successful StageMesh execution receipts. A role name supplied only inside model output is not execution identity. The approver identity is taken only from the approval-authority policy described below.

If StageMesh cannot determine a required execution identity at a gate boundary, the gate blocks. It must not substitute a provider display name, Git author, host user, or repository owner guess.

## Approval-authority policy

Accountable approval is enabled only by the presence of this object under the existing planning-gate configuration:

```json
{
  "approval_authority": {
    "version": 1,
    "authority_id": "project-owner:example",
    "approver_id": "owner:example"
  }
}
```

The object is strict: unknown keys, missing keys, invalid identities, or any version other than `1` are configuration errors. `authority_id` and `approver_id` use the canonical identity grammar. Version 1 intentionally names one accountable approver; multi-party or threshold approval is out of scope.

The authority digest is the lowercase SHA-256 of UTF-8 canonical JSON for the three fields above, with sorted keys and compact separators. The authority policy is read from the protected project configuration at the integration baseline, not from the candidate worktree. A candidate cannot change the authority that evaluates itself.

Changing any authority field invalidates existing approvals. Removal of `approval_authority` returns a legacy project to the existing opt-in behavior; it must produce a visible readiness downgrade and must not reinterpret a version-2 approval as a legacy approval. A later governed-profile specification may forbid that downgrade for governed projects; this specification does not activate or define that profile.

## Approval artifact version 2

The canonical approval artifact remains at:

```text
.stagemesh/planning/<task-id>.approval.json
```

With accountable approval configured, its exact schema is:

```json
{
  "version": 2,
  "decision": "approve",
  "task_id": "<task-id>",
  "planning_spec_hash": "<64 lowercase hex>",
  "planning_challenge_hash": "<64 lowercase hex or null>",
  "contract_hash": "<64 lowercase hex>",
  "baseline_sha": "<40 or 64 lowercase hex>",
  "authority_id": "<canonical identity>",
  "authority_hash": "<64 lowercase hex>",
  "approver_id": "<canonical identity>",
  "receipt_id": "<non-empty StageMesh receipt identifier>"
}
```

The object is strict and rejects unknown keys. `planning_challenge_hash` is a digest when the planning policy requires a challenge and is JSON `null` otherwise. An empty string is invalid. `decision` has only the value `approve` in version 2; rejection does not create an approval artifact.

The artifact is an inspectable projection, not sufficient authority by itself. The gate also requires one durable StageMesh audit receipt whose identifier and complete binding fields exactly match the artifact.

## Operator approval action and receipt

The implementation must provide one explicit operator action for recording approval. It may be exposed through the existing CLI, but it must use existing configuration and audit-storage mechanisms rather than introducing a second workflow engine.

The action must:

1. refuse to run from a StageMesh provider execution context;
2. accept a task identifier but no approver-identity override;
3. load and validate the current planning spec, required challenge, contract, baseline, and approval authority;
4. require an accepted challenge when the policy requires one;
5. refuse while implementation, review, or integration execution for that task is active;
6. record an append-only `planning_approval_recorded` audit event containing every version-2 artifact field except `receipt_id`, plus the generated receipt identifier and creation time;
7. atomically write the matching version-2 approval artifact only after the receipt is durable; and
8. return the task, approver, exact bound digests, baseline, authority digest, and receipt identifier to the operator.

The action must not modify the planning spec, challenge, task contract, task baseline, source issue, candidate branch, or project configuration. Repeating the action with identical inputs may return the existing receipt. A changed input creates a new receipt and artifact; it never mutates an earlier audit event.

Provider output, a hand-written approval file, an unmatched audit event, and a legacy version-1 approval are insufficient whenever `approval_authority` is configured.

## Role separation

At each enforcement boundary, StageMesh must build the set of successful execution identities applicable to the task and exact candidate:

- the planner that produced the frozen planning spec;
- the challenger that produced the accepted challenge, when required;
- the implementer selected for the initial or replacement implementation;
- every implementer identity inherited through candidate derivation; and
- the reviewer selected for final independent review.

The configured `approver_id` must differ from every identity in that set. Planner and challenger must remain distinct under the existing challenge rule. Reviewer independence remains governed by the existing review-identity control and is not replaced by this approval check.

The gate blocks before dispatch if the selected implementation or review actor would conflict with the approver. A retry, fallback, or recovery path must not select the approver as a replacement actor. Candidate derivation must not erase an earlier conflicting implementation identity.

## Enforcement boundaries

The accountable approval decision must be evaluated at all of these points:

| Boundary | Required outcome |
| --- | --- |
| Task pickup after contract binding | Validate the spec, challenge, authority, artifact, and receipt before any implementation actor runs. |
| Initial implementation dispatch | Resolve the implementation actor, then reject missing identity or role conflict before starting the process. |
| Resume, retry, fallback, or dead-execution recovery | Re-read current protected inputs and reject stale bindings or a conflicting replacement actor. Recovery cannot grandfather an earlier decision. |
| Review dispatch | Bind the exact candidate and reject a missing or conflicting reviewer identity before starting review. |
| Integration | Re-read the approval and receipt; require the exact approved plan, contract, baseline, authority, and candidate lineage. |
| Promotion or merge-policy handoff | Perform a final recheck and emit durable evidence of the accepted receipt and exact candidate SHA. |

Every refusal must leave task and candidate evidence intact. No refusal may silently relabel the task successful, synthesize approval, delete a worktree, or advance the lifecycle.

## Deterministic failure semantics

The implementation must expose stable machine-readable reasons at minimum for:

- `approval_missing`;
- `approval_version_unsupported`;
- `approval_malformed`;
- `approval_receipt_missing`;
- `approval_receipt_mismatch`;
- `approval_authority_missing`;
- `approval_authority_stale`;
- `approval_binding_stale`;
- `approval_actor_missing`;
- `approval_actor_unauthorized`;
- `approval_role_conflict`; and
- `approval_execution_identity_missing`.

Human-readable output must name the failed binding or conflicting roles without exposing credentials or provider prompts. The next action must be either to repair configuration, obtain a new approval for current inputs, or choose a non-conflicting execution actor. StageMesh must never suggest editing a digest by hand.

## Compatibility and migration

- No configuration is changed by this specification or its implementation.
- Without `approval_authority`, the existing planning-gate approval format and behavior remain unchanged.
- With `approval_authority`, only version 2 plus its matching receipt is accepted; there is no automatic conversion from a legacy file.
- Existing tasks are not rewritten. Their next accountable gate evaluation either passes with current version-2 evidence or blocks with a deterministic reason.
- The later `SDD-GAP-004` governed-profile work may require `approval_authority`; this specification only supplies the control.
- Enabling the control in StageMesh's live project configuration and running a governed canary belong to `SDD-GAP-001` or `SDD-GAP-004`, not this change.

## Required implementation scope

The later implementation issue may change only the smallest necessary subset of:

- `src/stagemesh/spec_planning.py` for strict schemas, canonical bindings, and the pure decision;
- `src/stagemesh/config.py` for the approval-authority policy;
- the existing CLI and audit-storage modules for the explicit operator action and durable receipt;
- `src/stagemesh/run_ready.py` and existing recovery/review/integration handoff modules for repeated enforcement;
- focused planning, recovery, role-identity, and CLI tests; and
- documentation describing the operator command and compatibility behavior.

The implementation issue must replace this category list with an explicit `allowed_files` list before it is marked ready. The implementation contract must exclude `docs/specs/**` and `docs/sdd-governance-audit-811.md`.

## Required verification

The implementation must include deterministic tests that prove:

1. a compliant version-2 artifact and matching durable receipt pass with distinct actors;
2. missing approver identity fails;
3. an unconfigured approver fails;
4. approver equal to planner fails;
5. approver equal to challenger fails;
6. approver equal to initial implementer fails before process start;
7. approver equal to a fallback or resumed implementer fails before process start;
8. approver equal to reviewer fails before review starts;
9. a copied artifact without its receipt fails;
10. a receipt with any mismatched binding fails;
11. changed spec, challenge, contract, baseline, or authority digest fails at resume and promotion;
12. a candidate change to project configuration cannot change the evaluating authority;
13. a provider execution cannot invoke the operator approval action successfully;
14. failed checks preserve task, candidate, execution, and worktree evidence;
15. legacy projects without `approval_authority` retain existing behavior; and
16. a complete synthetic StageMesh task with all actors distinct reaches the existing review and integration gates without bypass.

The final implementation review must compare code and tests against every numbered requirement in this file, on the exact candidate commit. Passing unrelated tests is not code-versus-spec verification.

## Non-goals

This specification does not:

- activate the planning gate, spec governance, or a governed profile;
- modify StageMesh's live project configuration;
- define multi-owner, threshold, delegated, expiring, or remote approval;
- provide cryptographic proof of the human at the keyboard or protection from a malicious process with the same host and database privileges as the operator;
- replace provider execution receipts, independent code review, integration tests, objective governance, branch protection, or hosted checks;
- resolve `SDD-GAP-001`, `SDD-GAP-003`, `SDD-GAP-004`, `SDD-GAP-005`, or `SDD-GAP-006`;
- authorize a Caventra task, canary, configuration change, architecture change, or product-code change; or
- treat a merged pull request, issue closure, model statement, or local JSON field as founder approval.

The trusted boundary for version 1 is the operator-controlled StageMesh configuration and durable store. Stronger host isolation or cryptographic human authentication requires a separately approved specification.

## Amendment rule

This file is immutable after it becomes authoritative. A requirement change requires a new issue, a new versioned specification file, independent exact-head review, founder approval, and a new merged commit and file digest. Existing approvals remain evidence for their original bindings but never authorize the amended version.
