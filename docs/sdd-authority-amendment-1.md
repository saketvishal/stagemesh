# SDD authority amendment 1 — founder trust root and READY-label policy

Status: `AWAITING_FOUNDER_SIGNATURE`.

This amendment records the founder's decisions of 2026-11-12 (America/Chicago). It does not imitate the required Ed25519 signature, enable `governed-sdd`, change the live project configuration, or authorize Caventra implementation.

## Authority

- Authority version: `sdd-authority-1`.
- Founder key id: `founder-saket-2026-01`.
- Algorithm: `ed25519`.
- Raw 32-byte public key, standard Base64: `KiXDMAbI54XmVZc/9bGTeHFdkRBd/MVUqMyRh746qxs=`.
- OpenSSH public-key fingerprint observed during registration: `SHA256:ilQQoRU2HFv+pQkludoKwoc5WFzPXELgXHOn1Xiel0M`.
- Private-key custody assertion: the corresponding private key is outside coding-agent, review-agent, CI, StageMesh runner, repository, worktree, project-config, and task-environment access.

The root trust document is `.stagemesh/approval-trust.json`. As specified by the approval bootstrap, this one root document is unsigned. It registers a verification key only. It approves no planning specification, task, profile, or candidate.

## D10 amendment

D10 is amended before signature. The original recommendation remains historical text in `docs/sdd-authority-approval-811.md`; it is not the accepted direction.

The accepted direction is:

- Set `github.auto_label_unlabeled` to `false` when governance is activated for StageMesh.
- Do not automatically apply the READY label to unlabeled issues.
- Keep PR-only landing, the GitHub ruleset, provider authorization, and the unmet Founder Hands-Off gate unchanged until their own approved changes.
- Do not treat this amendment, trust registration, or later synthetic certification as Caventra architecture approval or Caventra implementation authorization.

## Sequencing

1. Land this trust-root registration without enabling governance.
2. Use the resulting integration-baseline SHA to freeze the bounded implementation specification, independent challenge, strict contract, and canonical approval payload.
3. The founder signs those exact canonical payload bytes offline with `founder-saket-2026-01` and returns only the Base64 signature.
4. Verify the signature before implementing or activating the governed profile.
5. Keep merger authorization and any GitHub ruleset change as later, evidence-based founder decisions.

Until step 3 is complete, `sdd-authority-1` remains `AWAITING_FOUNDER_SIGNATURE`; governance stays off and no automatic labeling configuration changes are made.
