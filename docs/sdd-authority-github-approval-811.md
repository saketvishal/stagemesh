# SDD authority amendment — exact-head GitHub PR approval

Status: `AWAITING_FOUNDER_PR_APPROVAL`.

This amendment applies to `sdd-authority-1`. It records the founder's 2026-11-12 direction to remove the proposed Ed25519 approval system and use GitHub pull-request approval as the human authority boundary. It also records the founder's direction to disable automatic READY labeling.

Merging this document is the architecture approval act for this amendment. It does not activate governance, change the live queue, change the GitHub ruleset, or authorize Caventra implementation.

## Superseded complexity

The Ed25519 approval payload, approval trust file, key rotation, delegation signatures, private-key scanning, and signature-verification implementation described in `docs/sdd-authority-approval-811.md` and `docs/sdd-approval-bootstrap-811.md` are superseded. They remain historical proposal text and must not be implemented.

StageMesh does not generate, store, request, or use founder private keys. No `.stagemesh/approval-trust.json` is added.

## Approval authority

For a governed change, the approval source is the GitHub pull request containing the exact candidate or specification commit.

Admission requires all of the following:

1. The pull request belongs to the configured repository and targets the configured protected integration branch.
2. The pull request's current head SHA equals the SHA being admitted. A review of an earlier head is stale and grants nothing.
3. GitHub reports an `APPROVED` review submitted by a configured authorized approver for that impact class.
4. The approval is attached to the current head commit. A new commit, force-push, rebase, or head replacement invalidates it.
5. The approver is not the pull-request author and is not the implementation provider identity recorded for the candidate.
6. Required CI and the independent exact-SHA review status required by issue 808 pass on that same head SHA.
7. The bounded contract and planning specification are files in that reviewed head. Their normal digests remain evidence, but no second approval file or cryptographic signature is required.

GitHub unavailability, an unreadable review, a dismissed review, `CHANGES_REQUESTED`, a stale SHA, an unauthorized login, or same-author approval fails closed. It is a hold, not permission to bypass approval.

StageMesh records the repository, pull-request number, review id, reviewer login, review state, reviewed commit SHA, current head SHA, base branch, submission time, and source URL in audit evidence. It rechecks GitHub at admission and resume; a cached approval alone is insufficient.

## Roles

- Founder-only architecture and governance changes require approval from the configured founder GitHub login.
- A later scope owner may approve only an explicitly bounded implementation class assigned to that login in reviewed configuration.
- The initial configuration has no delegated scope owner.
- GitHub PR authorship must use an identity distinct from the founder approver. Until a bot, GitHub App, or another authorized author identity exists, the founder cannot provide an independent GitHub approval on a PR authored by the founder account.

## D10 amendment

When the StageMesh governed profile is activated:

- `github.auto_label_unlabeled` is `false`.
- Unlabeled issues are not automatically marked READY.
- PR-only landing remains required.
- Provider authorization, the Founder Hands-Off gate, and Caventra's separate architecture/specification gates remain unchanged.

## Controls retained from `sdd-authority-1`

- Mandatory planning and an independent challenge for governed changes.
- Explicit `STRICT` or `BOUNDED` scope; unclassified impact fails closed.
- Independent review on the exact candidate SHA.
- Task `DONE` remains separate from the objective verdict; only `ACHIEVED` means delivered.
- No challenge, validation, review, scope, or objective waiver.
- Project-owned runner worktrees and the existing operator-worktree rule.
- No OpenSpec import and no Caventra implementation authorization.

## Bounded implementation order

After this amendment is approved through GitHub:

1. Add the GitHub exact-head approval reader and fail-closed evidence checks by extending the existing planning/admission path; do not add a second approval store.
2. Complete issue 808 so the independent review produces a trusted required status bound to the same head SHA.
3. Connect the existing objective verdict to delivery reporting without redefining task `DONE`.
4. Enable the explicit StageMesh governed profile and set `github.auto_label_unlabeled` to `false` only after the preceding checks pass.
5. Run one isolated synthetic certification proving unauthorized work is blocked, approved work succeeds, and restart/recovery preserves evidence.
6. Request separate founder permission before merging implementation PRs or changing the GitHub ruleset.

No step starts Caventra development. Caventra still requires its own founder-approved architecture and bounded specification.
