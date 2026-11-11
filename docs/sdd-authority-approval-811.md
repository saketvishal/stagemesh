# Founder approval package — SDD governance, issue 811

Status: `AWAITING_FOUNDER_APPROVAL`.

This package asks for decisions. It does not activate a governed profile, change production code, or approve `sdd-authority-0`. Merging pull request 814 recorded the audit. That merge is not acceptance of the proposed requirements.

| Item | Value |
| --- | --- |
| Audit record | Pull request 814, head `3275fccae02aa01b29728e045895ec2dab6df2fc`, merge `edf7930e7fd04160cb231dae18c5ff4b827bdc24` |
| Audit document | `docs/sdd-governance-audit-811.md` |
| Proposed requirements | `SDD-AUTH-001` through `SDD-AUTH-015`, still `PROPOSED` |
| Recommended next baseline | `sdd-authority-1`, status `AWAITING_FOUNDER_APPROVAL` |
| Direction recorded | D1, D2, D6, D7, D8, D9, and D10 follow the recommendation below. D3 and D4 do not. D5 rejects both the original JSON name and the later git-identity recommendation. Those texts stay as history. The signoff package at the end is the candidate. |
| This file | One decision package. An agent must not edit it to imitate approval. |

## Reconciliation

Each existing requirement is kept. The disposition says what current StageMesh already does and what remains a founder choice.

| ID | Current fact | Conflict or gap | Recommended disposition |
| --- | --- | --- | --- |
| SDD-AUTH-001 | `README.md` describes a provider-neutral control plane and the task lifecycle, including a plan step. | The plan step is not the default gate. `config.py` leaves planning off when `planning_gate` is absent. | Keep the lifecycle text. Treat "plan" as mandatory only inside the governed profile. |
| SDD-AUTH-002 | `docs/founder-hands-off.md` says the gate is `NOT MET` and the clean streak is 0/10. | None with the other requirements. | Keep unmet. This package is not a streak task. |
| SDD-AUTH-003 | `evaluate_planning_gate` in `spec_planning.py` blocks implementation when `require_approved_spec` is true and the spec digest, contract digest, or baseline does not match. | The live project config has no `planning_gate`, so the check returns immediately. | Require it for the StageMesh governed profile. Leave it off for other projects. |
| SDD-AUTH-004 | A challenge can require a different planner and challenger, and only `accept` allows approval. `revise` and `needs-human-decision` block. | The challenge is separately opt-in and is off. Executed roles are proven only when `planner_provider` and `challenger_provider` are set. | Require the challenge and two distinct executed roles for nontrivial StageMesh work. |
| SDD-AUTH-005 | `docs/reliable-delivery.md` says executed roles must not approve themselves. | The gate accepts `approved: true` plus the matching digest. It does not record or compare an approver. | Add an accountable approver on the existing approval artifact. Do not add a new approval system. |
| SDD-AUTH-006 | Spec governance is an opt-in pinned adapter. Historical owners do not satisfy active dependencies. Closure of an issue is not founder approval. | This repository has no OpenSpec tree and no `spec_governance` config. The issue 769 qualification note still says that issue is open; GitHub shows it closed. | Do not adopt OpenSpec in the first baseline. Cite `SDD-AUTH-*` identifiers from planning specs. Treat the issue 769 sentence as a historical doc defect, not as an architecture choice. |
| SDD-AUTH-007 | Contracts can set explicit files, non-goals, invariants, and checks. `scope_mode` `STRICT` rejects wildcard write access. | The default allow is `**` when a contract omits `scope_mode`. | Governed StageMesh contracts use explicit files and `STRICT` or `BOUNDED`. Legacy contracts stay readable. |
| SDD-AUTH-008 | StageMesh task worktrees default to `<project>/.stagemesh/worktrees`. Broad roots, home, the checkout, and the project parent are rejected. | A configured root under the user temp directory is not in that denylist. Existing task worktrees must stay where they are. | Keep the default root. Reject configured temp roots. Do not relocate current worktrees. |
| SDD-AUTH-009 | Operator worktrees for this repository were placed under `C:\development\stagemesh\worktrees\issue-<id>`. | An agent can still run `git worktree add` in the Windows temp directory. The runner does not see that command. | Keep the operator rule. Do not pretend the product allocator enforces it. |
| SDD-AUTH-010 | Independent review defaults on. The review pool excludes the implementer. An unavailable reviewer holds the task under the current adverse policy. | The same provider names may appear in both pools. One usable provider cannot review itself. `invariant_review_unavailable` is not, on this base, the durable external-dependency hold. | Keep independent review. Do not redesign the issue 803 hold in this package. |
| SDD-AUTH-011 | Tracked `.stagemesh/agent-policy.json` and `.stagemesh/quality-policy.json` bind candidates from the integration baseline. | None for the governed profile. A candidate that edits either file cannot authorize itself. | Keep both policies. The governed profile does not replace them. |
| SDD-AUTH-012 | Live merging mode is `pr_only`. Required check names are `linux`, `windows`, and `Attribution policy`. | A workflow file does not prove GitHub branch protection. | Keep PR-only landing. Do not change the ruleset in this package. |
| SDD-AUTH-013 | `objective_governance.py` can return `ACHIEVED`, `NOT_ACHIEVED`, or `INCONCLUSIVE`. Child completion and test counts do not decide it. | Nothing in the task path calls that module. Task `DONE` means contract, validation, review, and integration. | Keep both states. Do not make task `DONE` mean the product objective is complete. |
| SDD-AUTH-014 | A changed planning spec gets a new digest. When the gate is on, a stale digest blocks admission and resume. | The gate is off, and the approval file has no actor. An implementer who can write the approval file can bind a new digest. | Keep immutable versions. Pair them with the approver check from SDD-AUTH-005. |
| SDD-AUTH-015 | Other projects keep today's opt-in defaults. | Issue 811 asks for a mandatory profile for StageMesh's own nontrivial development. A silent global default would break that compatibility. | Mandatory for this project's nontrivial development through an explicit profile value. Default for every other project remains off, with a visible downgrade when a project is not governed. |

## Unresolved choices

These are the contradictions the code and documents do not already settle.

1. `README.md` prints a plan step. The config default skips it. The profile split above is the resolution, if the founder accepts it.
2. `docs/reliable-delivery.md` requires a human approval. The checker does not know who approved. That is decision D5.
3. A finished task and a finished objective are different machines. Issue 811 says a unit-green change that misses the original objective is not delivered. The current runner can still mark the task `DONE`. That is decision D6.
4. StageMesh has two legal worktree locations: the product default under `.stagemesh/worktrees`, and the operator directory `C:\development\stagemesh\worktrees`. Temp directories are a third location the product does not reject. That is decision D7.
5. There is no single governed switch. Turning on planning, challenge, spec audit, atomic scope, and objective evaluation independently can leave a project half-governed. That is decision D2.
6. This repository has no OpenSpec corpus. Pointing `spec_governance` at OpenSpec would create a second requirements tree. That is decision D8.
7. `docs/development-readiness-qualification.md` disagrees with the closed state of issue 769. That is a stale sentence, not a second architecture.

No requirement in `SDD-AUTH-001` through `SDD-AUTH-015` needs to be deleted. SDD-AUTH-006 and SDD-AUTH-015 stay true for projects that do not adopt the profile.

## Recommended baseline

Version `sdd-authority-1`. Owner: founder. Status of this recommendation: `AWAITING_FOUNDER_APPROVAL`.

Until the founder accepts it, `sdd-authority-0` remains the unapproved proposal in the audit, and runtime behavior stays as it is. Accepting `sdd-authority-1` means the signoff package, including the recorded direction, the D3 and D4 amendments, and the D5 signed-approval decision. It still does not flip the live config by itself. A later, separate authorization implements them. This commit is not that acceptance.

The baseline reuses the current control plane:

- planning specs, challenges, and approvals in `spec_planning.py`
- executed planner and challenger receipts in `planning_execution.py`
- admission and resume checks in `run_ready.py` and `recovery.py`
- contracts, `STRICT` scope, and quarantine in `contracts.py` and validation
- baseline agent and quality policies
- independent review in `review.py`
- objective artifacts in `objective_governance.py`
- worktree roots in `workspaces.py` and `config.py`

It does not add a second specification engine, a second reviewer lifecycle, or a new objective store.

## Governed profile

Name: `governed-sdd`.

Not enabled by this document. The path rule, the Claude/Antigravity pair, and the approver-name sentence in this section are the original proposal. The signoff package amends them. Do not implement from this section.

A project is governed only when its config explicitly sets that profile. Detecting `saketvishal/stagemesh` and turning the profile on automatically is rejected, because a fork or a historical checkout would change behavior without a decision. The StageMesh repository's own nontrivial development becomes mandatory only when the founder later authorizes the config change for `C:\development\stagemesh\main`.

When the profile is on, implementation dispatch requires all of the following:

- an approved planning spec bound to the contract digest and baseline SHA
- an independent challenge with verdict `accept` and a different challenger from the planner
- execution receipts when planner and challenger providers are configured
- an approval whose approver is present and is not the planner, challenger, or implementer
- a contract whose write set is explicit and whose `scope_mode` is `STRICT` or `BOUNDED`
- the existing baseline agent policy, quality policy, exact-SHA validation, and independent review

When the profile is absent, behavior stays the current opt-in default. A doctor or audit report should say that the project is on the legacy path. That statement is a warning, not a silent relaxation of a project that opted in.

Nontrivial StageMesh work, once the founder makes the profile mandatory here, is a task whose allowed files intersect `src/**`, `scripts/**`, `.github/**`, or the tracked `.stagemesh` policy JSON files. A docs-only change that does not amend `SDD-AUTH-*` text or this baseline may omit the plan challenge. It may not omit independent review when the repository's normal review rule applies, and it may not change authority identifiers.

Codex is not a required role. The recommended pair, because both are already authorized for this project, is planner `claude` and challenger `agy`. The founder can name the opposite pair. The two names must differ.

## Workspace-root policy

Two rules, both already stated, plus one new refusal the founder must accept before any code change.

| Class | Required root | Enforcer |
| --- | --- | --- |
| StageMesh runner task worktrees | `<project>/.stagemesh/worktrees`, unless an approved config selects another project-owned root | Existing `runtime.worktree_root` |
| Operator and agent worktrees for this repository | `C:\development\stagemesh\worktrees\issue-<id>`, with `-review` and `-evidence` siblings | Operator rule. Not a runner allocation. |
| Configured runtime root under the process temp directory or `%LOCALAPPDATA%\Temp` | Refused | Proposed extension of `_validate_worktree_root`. Not implemented. |

Existing directories under `C:\development\stagemesh\main\.stagemesh\worktrees` stay in place. `runtime.allow_unsafe_worktree_root` remains an explicit override, not the default. A direct `git worktree add` outside the runner is still outside product enforcement. The first implementation slice does not add a Git hook.

## Decisions for the founder

The text under each decision is the original recommendation and stays as history. Direction on D1, D2, D6, D7, D8, D9, and D10 matches that recommendation. D3 and D4 were amended in the signoff package. D5 was sent back, and the later git-identity recommendation was rejected. The signature decision is in the signoff package. The package remains `AWAITING_FOUNDER_APPROVAL`.

### D1. Record this package as the candidate baseline

Recommendation: accept `sdd-authority-1` as the candidate and leave `sdd-authority-0` `PROPOSED` until that acceptance. Do not edit the audit in place.

Accept: later implementation has one versioned target. Reject: issue 811 stays at the audit, and agents have no newer authority than `sdd-authority-0`.

### D2. Make `governed-sdd` explicit and mandatory only for this project's nontrivial work

Recommendation: explicit config value. Other projects stay on today's defaults. No repository-name auto-switch.

Accept: StageMesh development can be held to the profile without changing Caventra or other legacy projects. Reject: either every project changes, or StageMesh itself stays opt-in and issue 811's mandatory profile is unmet.

### D3. Define nontrivial as product, script, workflow, and policy files

Original recommendation, not accepted: the path intersection in the governed-profile section. Docs-only records that do not change `SDD-AUTH-*` text may skip the plan challenge.

Amendment required before signoff: classify by behavioral and policy impact, including architecture docs, agent instructions, build configuration, tests, and protected governance files. The amendment is in the signoff package. This paragraph stays as the record of the first recommendation.

Accept: audit and approval documents can land without a false implementation gate. Reject the docs exception: every markdown edit waits for a challenge. Widen nontrivial to all files: generated evidence and lockfiles enter the gate.

### D4. Require an independent challenge before implementation

Original recommendation, not accepted as an architectural dependency: `require_approved_spec` and `require_plan_challenge` together, with distinct executed planner and challenger providers. Recommended names: `claude` then `agy`.

Amendment required before signoff: require independent authorized planner and challenger identities. Claude and Antigravity are preferred providers, not immutable dependencies. The amendment is in the signoff package. This paragraph stays as the record of the first recommendation.

Accept: a missing, stale, self-reviewed, or non-accept challenge blocks dispatch. Reject: a spec can be approved with no independent challenge, which fails issue 811's pre-code challenge requirement. Requiring Codex as one of the roles conflicts with Codex remaining optional.

### D5. Require an accountable approver on the existing approval file

Original recommendation, not accepted: extend the current `.approval.json` consumed by `evaluate_planning_gate` with `approver_name` and `approver_role`. Authority-baseline changes require role `founder`. Ordinary task specs require role `scope-owner`. The gate rejects a missing approver and an approver whose provider identity matches the planner, challenger, or implementer. The planner and challenger still cannot write that file. Do not add signatures or a second approval store in the first slice.

Amendment required before signoff: a self-written name or role in a JSON file is not approval. Approval must be an independently verified operator act, bound to the frozen specification digest and the applicable authority version. The amendment is in the signoff package. This paragraph stays as the record of the first recommendation.

Later recommendation, also not accepted: treat a matching git author and committer from `git_identity.py` as the approval. That text stays under the historical unresolved-choice subsection. The resolved D5 direction is an operator-controlled Ed25519 signature. It is recorded in the signoff package and is still not a founder signature.

Accept: self-approval by digest flag stops being sufficient. A person who can write files locally can still type a false name; the process receipts catch the known provider identities, not every local writer. Reject: the documented human-approval rule stays unenforced. Choose signatures now: the first slice becomes a new trust subsystem, which this package rejects.

### D6. Keep task completion and objective completion apart

Recommendation: a child task may reach `DONE` when its contract, validation, review, and integration pass. The original objective stays in `objective_governance.py`. `ACHIEVED` is the only product-complete claim. `NOT_ACHIEVED` and `INCONCLUSIVE` remain visible. Passing tests and finished child tasks do not change that verdict. The runner must not report the objective delivered in those cases. Do not block an individual child task's `DONE` only because the parent objective is still open.

Accept: issue 811's "not delivered" result has an existing verdict to use. Reject and block task `DONE` whenever an objective is open: one unfinished criterion stalls every child. Reject and leave objective evaluation unwired: unit-green tasks can be described as product delivery.

### D7. Enforce the product worktree root, and keep the operator root as an operator rule

Recommendation: keep `<project>/.stagemesh/worktrees`. Refuse a configured root inside the process temp directory or `%LOCALAPPDATA%\Temp`. Keep `C:\development\stagemesh\worktrees\issue-<id>` as the operator location for agent-created worktrees. Do not retarget the live root and do not move existing runtime worktrees.

Accept: StageMesh-managed worktrees cannot be pointed at temp without the unsafe override. Agents that bypass the runner can still create a temp worktree. Reject the temp refusal: the issue 804 class of misconfiguration stays legal. Retarget the product root to `C:\development\stagemesh\worktrees`: current task worktrees under `.stagemesh\worktrees` would no longer match the allocator.

### D8. Do not create an OpenSpec corpus

Recommendation: planning `spec_refs` cite `SDD-AUTH-*` and the accepted baseline document. Structural spec-governance stays available and off until a later slice needs a `custom-json` export of those same identifiers. Do not import OpenSpec as the StageMesh architecture authority.

Accept: one requirements list, the `SDD-AUTH` identifiers, remains the list to amend. Reject and adopt OpenSpec now: a second tree must be written and kept aligned before any governed task can start.

### D9. No first-slice bypass of challenge, review, or validation

Recommendation: an urgent fix still needs an approved scope, an accountable approver, independent review, and the contract's tests. It does not skip the challenge. A later waiver would be a new decision.

Accept: hotfix latency includes one challenge. Reject and allow a challenge waiver: the waiver needs its own owner, reason, and expiry, and it can become the usual path.

### D10. Leave labeling, landing, and the unmet hands-off gate unchanged

Recommendation: do not change `github.auto_label_unlabeled`, PR-only merging, the ruleset, provider authorization, or the Founder Hands-Off streak. With the planning gate on, a ready label still cannot dispatch code without the spec evidence. Do not claim this package advances the 0/10 streak.

Accept: admission and delivery policy stay where they are. Reject and disable auto-label: issue intake changes in the same decision as the spec gate. Treat this package as hands-off proof: the gate document would be false.

## Bounded implementation plan

Historical plan from the first package. Not authorized. Slice 1 below repeats the rejected D5 mechanism and is not the signoff boundary. The git-identity channel is not the boundary either. The signoff package replaces this table for any later implementation. The rows stay so the first proposal remains visible.

| Slice | Reuse | Change | Proof | Does not include |
| --- | --- | --- | --- | --- |
| 1 | `evaluate_planning_gate` and `tests/test_spec_planning_gate.py` | Require `approver_name` and `approver_role`, and reject a matching planner, challenger, or implementer identity. | Existing planning-gate tests plus the self-approval refusal. | Turning the gate on for this project. |
| 2 | `_planning_gate_policy` | Add an explicit `governed-sdd` profile that requires the existing planning flags and distinct providers, and fails closed when the combination is incomplete. Absent profile keeps today's defaults. | Config tests for on, off, and incomplete. | Editing the gitignored live config. |
| 3 | `objective_governance.py` | Report the existing objective verdict at delivery. Do not derive it from task `DONE` or test counts. | One synthetic objective that stays `NOT_ACHIEVED` or `INCONCLUSIVE` when component tests pass. | Marking issue 420 or Founder Hands-Off met. |
| 4 | `_validate_worktree_root` | Reject a configured root inside the process temp directory or `%LOCALAPPDATA%\Temp`. | Config rejection tests. No relocation. | A Git hook for operator `git worktree add`. |
| 5 | `run_ready.py`, contracts, review, validation | One synthetic governed task outside the live queue. Negative cases block. One compliant case reaches the existing gates. | Runner evidence bound to the exact candidate SHA. | Caventra files, a batch, or auto-labeling unrelated issues. |
| 6 | This project's gitignored config | Set `governed-sdd` only after slices 1 through 5 pass and the founder authorizes that config change. | Doctor or audit output shows the profile on, and a missing spec blocks. | A code change hidden in the same edit. |

The issue 769 wording fix is a separate documentation correction. It is not a slice of this baseline.

## Founder signoff package

Status of `sdd-authority-1`: `AWAITING_FOUNDER_APPROVAL`.

This is the final candidate. It is not a second architecture. `SDD-AUTH-001` through `SDD-AUTH-015` remain the requirements, with the provenance in the reconciliation table. Direction on D1, D2, D6, D7, D8, D9, and D10 is the recommendation already written above. D3 and D4 apply as previously amended. D5 applies only as resolved in this section. Nothing here enables the profile, changes live configuration, or records a founder signature.

### Authority requirements

| Rule | Statement |
| --- | --- |
| Baseline | `sdd-authority-1` becomes authoritative only when the founder approves this signoff package. `sdd-authority-0` stays `PROPOSED`. The audit file is not edited to simulate that approval. |
| Profile | `governed-sdd` is an explicit config value. It is the intended rule for nontrivial StageMesh development. Other projects stay on today's opt-in defaults. The repository name does not turn the profile on. |
| Governed change | A change is governed when it can alter behavior or policy. That class includes architecture documents, agent instructions, build configuration, tests, and protected governance files, as well as product source and scripts. Path filters that cover only `src/**` are not sufficient. A change whose impact is unclassified is governed. |
| Not a governed behavior change | Generated evidence, dependency lockfiles that do not change policy, and historical release notes that do not amend architecture, agent instructions, or `SDD-AUTH-*` text. When a file mixes those with a governed class, the whole change is governed. |
| Challenge | Implementation of a governed change waits for an independent challenge whose verdict is `accept`. The planner and challenger are two different authorized provider identities, with different command identities. Claude and Antigravity are the preferred pair when both are authorized and available. Their names are not required forever, and this package does not add or remove any provider authorization. Codex is not required. |
| Approval | Approval is an Ed25519 signature by an operator-controlled key over the payload below. Git name and email are not authorization. A name or role written into `.approval.json` is not approval. The founder signs authoritative architecture changes. A scope owner may sign only a bounded implementation spec inside a founder-signed delegation. |
| Task and objective | A task may reach `DONE` through its contract, validation, review, and integration. The original objective remains a separate verdict. `ACHIEVED` is the only product-complete claim. `NOT_ACHIEVED` and `INCONCLUSIVE` must not be reported as delivery. |
| Workspace | Runner worktrees stay under `<project>/.stagemesh/worktrees`. A configured root in the process temp directory or `%LOCALAPPDATA%\Temp` is refused. Operator worktrees for this repository stay under `C:\development\stagemesh\worktrees\issue-<id>`. Existing runtime worktrees are not moved. |
| Authority corpus | Planning specs cite `SDD-AUTH-*` and the accepted baseline. OpenSpec is not adopted. |
| No bypass | The first governed profile has no waiver for the challenge, independent review, or required validation. |
| Unchanged controls | Auto-label, PR-only landing, the GitHub ruleset, provider authorization, and the unmet Founder Hands-Off gate stay as they are. |

### Enforcement sequence

This sequence is the required order after a later implementation authorization. It is not enabled now.

1. Classify the change by behavioral and policy impact. Unclassified impact fails closed as governed.
2. For a governed change, require a frozen planning spec bound to the contract digest and baseline SHA, citing the applicable `SDD-AUTH-*` identifiers and the authority version.
3. Require a challenge from a second authorized provider identity. Prefer Claude and Antigravity. Accept another distinct authorized pair when a preferred provider is unavailable. A missing, stale, same-identity, or non-`accept` challenge blocks dispatch.
4. Require an Ed25519 approval over the payload below. Verify it with the baseline public key only. Bind it to the authority version, frozen spec digest, required challenge digest, contract digest, baseline SHA, approver identity, role, and decision. Reject a missing, stale, forged, incorrectly scoped, revoked, or self-written approval. Git name and email do not satisfy this step.
5. Admit implementation only inside an explicit `STRICT` or `BOUNDED` contract. The existing agent policy, quality policy, and worktree-root rules apply.
6. Validate and independently review the exact candidate SHA. The reviewer is not the implementer.
7. The task may then reach `DONE`. Report the objective verdict separately. Do not report the objective delivered when that verdict is `NOT_ACHIEVED` or `INCONCLUSIVE`.

### Historical unresolved choice

The text in this subsection is the earlier D5 recommendation. It is not the decision.

One choice remains inside the amended D5, and it blocks implementation of the approval check until the founder selects it. A self-written JSON role is already rejected.

Recommendation: reuse `git_identity.py` and the existing attribution identity check. Accept the approval only when its author and committer are the configured operator identity, those identities are not tool or provider identities, and the recorded payload contains the frozen spec digest, the challenge digest when required, and the authority version. Planner, challenger, and implementer processes cannot create that record.

Consequence of accepting that recommendation: an operator git commit is the verification channel, and a stolen operator identity can approve. Consequence of rejecting it: a stronger channel, such as an out-of-band signature, has to be chosen before any approval slice is written. This package does not add that channel.

No other decision in D1 through D10 remains open except final approval of this package and that D5 channel.

### D5 resolution

That recommendation is rejected. Git author name, committer name, and email are not authorization. `git_identity.py` stays the attribution check. It is not the approval check.

The approval mechanism is an operator-controlled Ed25519 signature, verified by the existing planning gate. The private key is not available to coding agents, review agents, or StageMesh task execution. The runner receives only the public verification key. No D1 through D10 design choice remains open inside this package. `sdd-authority-1` stays `AWAITING_FOUNDER_APPROVAL` until a founder signature that meets this payload is recorded. This commit does not contain that signature.

### Approval payload

The approval file remains `.stagemesh/planning/<task_id>.approval.json`, the path `planning_artifact_paths` already returns. `load_planning_artifacts` remains the reader. `planning_execution.py` still never writes the file. `planner_scope_violations` and `plan_challenger_scope_violations` still forbid it. When `governed-sdd` is on, `{ "approved": true, "planning_spec_hash": ... }` is not approval. When the profile is absent, that legacy object keeps today's meaning.

The file is one object with `version` equal to `1` and a `records` array. Each record is an envelope:

- `payload`: the signed object below, and no other fields
- `algorithm`: `ed25519`
- `public_key`: standard base64 of the 32-byte raw Ed25519 public key
- `signature`: standard base64 of the 64-byte raw Ed25519 signature

The signed bytes are the UTF-8 JSON of `payload` only. Object keys are sorted at every level. Separators are `","` and `":"`. There is no trailing newline. This is the same canonical form `_canonical_json` uses. The signature covers those bytes and nothing else. Any other algorithm, length, encoding, or extra field fails closed.

| Field | Required value |
| --- | --- |
| `version` | `1` |
| `authority_version` | `sdd-authority-1` for an approval under this baseline. Any other value fails closed until a later accepted baseline names its own version. |
| `planning_spec_hash` | Lowercase SHA-256 from `freeze_planning_spec`. |
| `planning_challenge_hash` | Lowercase SHA-256 from `freeze_planning_challenge` when a challenge is required. Null or a missing field fails closed for a governed change. |
| `contract_hash` | 64 lowercase hex, equal to the frozen spec and to the gate's contract digest. |
| `baseline_sha` | 40 or 64 lowercase hex, the same forms the current gate accepts, equal to the frozen spec and to the gate's baseline. |
| `approver_id` | Id of the public key in the baseline trust set. Not a display name, git name, or email. |
| `approver_role` | `founder` or `scope-owner`. It must match the trust record for that public key. |
| `decision` | `approve`, `amend`, `supersede`, or `revoke`. |
| `subject_task_id` | The task id in the approval path. A record for another task fails closed. |
| `delegation_id` | Null when the role is `founder`. For `scope-owner`, the SHA-256 of the canonical founder-signed delegation payload. |
| `prior_record_hash` | Null only on the first record. Otherwise the SHA-256 of the previous record's canonical payload. |
| `revokes_record_hash` | Null except on `revoke`, where it is the SHA-256 of the payload being withdrawn. |

A record hash is the SHA-256 of that same canonical payload. The gate accepts a grant only when every record in the file verifies, each `prior_record_hash` matches the previous payload, and the active record is the latest `approve`, `amend`, or `supersede` that matches the current spec, required challenge, contract, baseline, and authority version, and that no later valid `revoke` names. A broken, reordered, or truncated chain fails closed even if the last signature is valid. A `revoke` record authorizes nothing.

The founder signs authoritative architecture changes. That class is `SDD-AUTH-*` text, the accepted authority baseline, architecture documents, agent instructions, protected governance files, and any unclassified impact. A scope owner may sign a bounded implementation spec only when a founder-signed delegation names that owner's public key, role `scope-owner`, impact `bounded-implementation`, and an explicit path list. The spec's affected paths must be inside that list. The list must not contain `**`. A mixed change that includes any founder-only class requires the founder. A scope owner cannot sign a delegation, the trust file, or their own wider authority.

Delegations use this same canonical signature. They live in the trust data below, not in a second approval store.

### Trust boundary

Verification is a check inside `evaluate_planning_gate`. Signing is not a StageMesh command, not a provider role, and not a function of `planning_execution.py`.

| Inside the runner | Outside the runner |
| --- | --- |
| Baseline public keys, signature verification, digest comparison, role and delegation checks, fail closed | Private keys and the act of signing |
| Existing spec, challenge, contract, and baseline identity | Git author and committer. Attribution may still check them. They do not approve. |
| Refusal when private-key material is present in the task workspace, config, or task environment | Operator-controlled devices that hold the private keys |

A mathematically valid signature under a key that is present to an agent fails closed. An extra name field fails closed. A planner, challenger, or implementer cannot satisfy the check by using another name or a credential that its process can read. The public key is loaded from the integration baseline. A candidate that edits the trust file does not get to verify its own approval with the edited key. That is the same baseline rule already used for `.stagemesh/agent-policy.json` and `.stagemesh/quality-policy.json`.

The trust data is `.stagemesh/approval-trust.json`. It is input to the existing gate, not a new governance product. It holds public keys and founder-signed delegations. It holds no private key. Changing it is an architecture change. This package does not create the file and does not put a key in it.

### Key-custody assumptions

- The founder private key, and each scope-owner private key, stays on a device the operator controls. That device is not a coding agent, a review agent, CI, a task worktree, or a `run-window` environment.
- The repository, the gitignored project config, provider commands, and the runner store neither a private key nor a path to one.
- This commit contains no public key and no private key. Registration waits for a later authorized slice and a separate founder signature.
- A stolen private key can approve until the founder revokes that public key with a signature under a key that is still active. The signature shows key custody. It does not add a second presence test.
- Replacing a lost founder key is a later founder-signed amendment of the trust data. This package does not define a backup ceremony.
- No signing subcommand is added. An agent must not be given a way to produce the signature.

### Implementation boundaries

Not authorized. After a later founder signature and a separate implementation authorization, changes stay inside these bounds. They reuse `spec_planning.py`, `planning_execution.py`, `run_ready.py`, `recovery.py`, `contracts.py`, `review.py`, `objective_governance.py`, `workspaces.py`, and `config.py`. They do not add a specification engine, an approval service, or a signing command.

| Allowed later | Forbidden |
| --- | --- |
| Impact classification ahead of `evaluate_planning_gate`, failing closed when unclassified | A path list limited to `src/**`, `scripts/**`, `.github/**`, and policy JSON |
| Distinct authorized planner and challenger identities, with Claude and Antigravity as preference only | Hard-coding Claude or Antigravity as the only legal roles, or changing the authorized provider list |
| Ed25519 verification of the payload above inside the existing approval file and gate | Treating git name or email, or `approver_name` / `approver_role` in agent-written JSON, as approval |
| Public keys and delegations in `.stagemesh/approval-trust.json`, read from the integration baseline | A private key in the runner, a second approval store, or a StageMesh signing command |
| Objective verdict reported beside task `DONE` | Making task `DONE` mean the product objective is complete, or blocking every child because the objective is open |
| Temp-root refusal in `_validate_worktree_root` | Retargeting `runtime.worktree_root` or moving existing `.stagemesh/worktrees` |
| Explicit `governed-sdd` config, default absent | Enabling it in the live project config, auto-enabling it by repository name, or editing provider authorization |
| One synthetic canary after those checks exist | New GitHub issues, coding agents, a batch, Caventra edits, an OpenSpec import, or a ruleset change |

### Implementation slices

Not authorized. None of these slices starts from this commit.

| Slice | Reuse | Change | Does not include |
| --- | --- | --- | --- |
| 1 | `evaluate_planning_gate`, `load_planning_artifacts`, `tests/test_spec_planning_gate.py` | Verify the Ed25519 envelope in the existing approval file. Under `governed-sdd`, reject the legacy `approved: true` object. Leave that object valid when the profile is absent. | A live key, a trust file in the repository, or turning the profile on. |
| 2 | Baseline policy load used for `.stagemesh/agent-policy.json` and `.stagemesh/quality-policy.json` | Read public keys and delegations from `.stagemesh/approval-trust.json` on the integration baseline. Refuse governed admission when private-key material is in config, the task environment, or the task workspace. | Accepting a candidate's own edit of the trust file as its verification key. |
| 3 | The same gate and the D3 impact class | Founder signature for architecture and unclassified impact. Scope-owner signature only inside a founder-signed delegation and its explicit paths. | A scope owner signing the trust file, a delegation, or an architecture change. |
| 4 | The same `.approval.json` | Append-only `records` for approve, amend, supersede, and revoke. A broken `prior_record_hash` chain fails closed. `planning_execution.py` still does not write the file. | A second history database or rewriting an older record. |
| 5 | `_planning_gate_policy`, `objective_governance.py`, `_validate_worktree_root` | Add the explicit `governed-sdd` flag, default absent. Report the existing objective verdict beside task `DONE`. Reject a configured temp root. | Editing the gitignored live config or moving worktrees. |
| 6 | `run_ready.py`, contracts, review, validation | One synthetic governed task outside the live queue. | Caventra, a batch, auto-labeling, or a new GitHub issue. |

### Acceptance tests

These tests are the proof for slices 1 through 4. They are not implemented now. Extend `tests/test_spec_planning_gate.py` rather than adding a new governance package.

- A founder signature over the canonical payload, with matching spec, challenge, contract, baseline, and `sdd-authority-1`, allows the governed gate.
- A flipped signature bit, a truncated signature, a non-Ed25519 algorithm, or a public key that is not in the baseline trust set is rejected.
- An unknown payload field, including `approver_name`, is rejected. A different display name cannot approve.
- A missing record, an unreadable approval file, or a legacy `{ "approved": true }` object is rejected while the governed check is on, and the legacy object still passes while that check is off.
- A mismatch of `planning_spec_hash`, `planning_challenge_hash`, `contract_hash`, `baseline_sha`, or `authority_version` is rejected as stale.
- A governed approval with a null challenge digest is rejected.
- A scope-owner signature on architecture text, agent instructions, protected governance, the trust file, or an unclassified change is rejected.
- A scope-owner signature whose affected paths are outside the founder-signed delegation, or whose delegation contains `**`, is rejected.
- A scope-owner signature inside that delegation, for a bounded implementation spec, is accepted.
- A valid signature is rejected when the corresponding private key is placed in the task workspace, the task environment, or the runner config.
- The verification path has no private-key input. `planning_execution.py` still cannot write `.approval.json`.
- A planner or challenger scope that includes the approval file or the trust file remains a scope violation.
- A candidate edit to `.stagemesh/approval-trust.json` is not the key set used to admit that candidate.
- An `amend` or `supersede` record is the active grant only when it matches the current digests and its `prior_record_hash` links to the previous payload.
- A later valid `revoke` record makes the named grant fail closed and does not itself authorize implementation.
- Deleting, reordering, or replacing an earlier record fails closed even when the newest signature is valid.
- A matching git author and committer, with no valid signature, is rejected.

## What this package does not do

- It does not mark `sdd-authority-0` or `sdd-authority-1` approved.
- It does not record a founder signature or register a public or private key.
- It does not implement Ed25519 verification or add a signing command.
- It does not enable `governed-sdd` or change `C:\development\stagemesh\main\.stagemesh\config.json`.
- It does not change provider authorization or require Codex.
- It does not create an implementation issue, start a worker, or label an issue ready.
- It does not invoke a coding agent.
- It does not move the shared checkout or any existing task worktree.
