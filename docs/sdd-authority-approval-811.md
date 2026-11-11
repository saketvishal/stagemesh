# Founder approval package — SDD governance, issue 811

Status: `AWAITING_FOUNDER_APPROVAL`.

This package asks for decisions. It does not activate a governed profile, change production code, or approve `sdd-authority-0`. Merging pull request 814 recorded the audit. That merge is not acceptance of the proposed requirements.

| Item | Value |
| --- | --- |
| Audit record | Pull request 814, head `3275fccae02aa01b29728e045895ec2dab6df2fc`, merge `edf7930e7fd04160cb231dae18c5ff4b827bdc24` |
| Audit document | `docs/sdd-governance-audit-811.md` |
| Proposed requirements | `SDD-AUTH-001` through `SDD-AUTH-015`, still `PROPOSED` |
| Recommended next baseline | `sdd-authority-1`, in force only after the founder accepts the decisions below |
| This file | Decision package. An agent must not edit it to imitate approval. |

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

Until the founder accepts it, `sdd-authority-0` remains the unapproved proposal in the audit, and runtime behavior stays as it is. Accepting `sdd-authority-1` means the decisions D1 through D10 below. It still does not flip the live config by itself. A later, separate authorization implements them.

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

Not enabled by this document.

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

Each item is open. The recommended default is the one this package will treat as `sdd-authority-1` if the founder accepts the package as a whole. Rejecting one item keeps the package at `AWAITING_FOUNDER_APPROVAL` until that item is rewritten.

### D1. Record this package as the candidate baseline

Recommendation: accept `sdd-authority-1` as the candidate and leave `sdd-authority-0` `PROPOSED` until that acceptance. Do not edit the audit in place.

Accept: later implementation has one versioned target. Reject: issue 811 stays at the audit, and agents have no newer authority than `sdd-authority-0`.

### D2. Make `governed-sdd` explicit and mandatory only for this project's nontrivial work

Recommendation: explicit config value. Other projects stay on today's defaults. No repository-name auto-switch.

Accept: StageMesh development can be held to the profile without changing Caventra or other legacy projects. Reject: either every project changes, or StageMesh itself stays opt-in and issue 811's mandatory profile is unmet.

### D3. Define nontrivial as product, script, workflow, and policy files

Recommendation: the path intersection in the governed-profile section. Docs-only records that do not change `SDD-AUTH-*` text may skip the plan challenge.

Accept: audit and approval documents can land without a false implementation gate. Reject the docs exception: every markdown edit waits for a challenge. Widen nontrivial to all files: generated evidence and lockfiles enter the gate.

### D4. Require an independent challenge before implementation

Recommendation: `require_approved_spec` and `require_plan_challenge` together, with distinct executed planner and challenger providers. Recommended names: `claude` then `agy`.

Accept: a missing, stale, self-reviewed, or non-accept challenge blocks dispatch. Reject: a spec can be approved with no independent challenge, which fails issue 811's pre-code challenge requirement. Requiring Codex as one of the roles conflicts with Codex remaining optional.

### D5. Require an accountable approver on the existing approval file

Recommendation: extend the current `.approval.json` consumed by `evaluate_planning_gate` with `approver_name` and `approver_role`. Authority-baseline changes require role `founder`. Ordinary task specs require role `scope-owner`. The gate rejects a missing approver and an approver whose provider identity matches the planner, challenger, or implementer. The planner and challenger still cannot write that file. Do not add signatures or a second approval store in the first slice.

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

Not authorized. After the founder accepts the decisions, each slice is a separate reviewed change. None of these slices creates a new subsystem.

| Slice | Reuse | Change | Proof | Does not include |
| --- | --- | --- | --- | --- |
| 1 | `evaluate_planning_gate` and `tests/test_spec_planning_gate.py` | Require `approver_name` and `approver_role`, and reject a matching planner, challenger, or implementer identity. | Existing planning-gate tests plus the self-approval refusal. | Turning the gate on for this project. |
| 2 | `_planning_gate_policy` | Add an explicit `governed-sdd` profile that requires the existing planning flags and distinct providers, and fails closed when the combination is incomplete. Absent profile keeps today's defaults. | Config tests for on, off, and incomplete. | Editing the gitignored live config. |
| 3 | `objective_governance.py` | Report the existing objective verdict at delivery. Do not derive it from task `DONE` or test counts. | One synthetic objective that stays `NOT_ACHIEVED` or `INCONCLUSIVE` when component tests pass. | Marking issue 420 or Founder Hands-Off met. |
| 4 | `_validate_worktree_root` | Reject a configured root inside the process temp directory or `%LOCALAPPDATA%\Temp`. | Config rejection tests. No relocation. | A Git hook for operator `git worktree add`. |
| 5 | `run_ready.py`, contracts, review, validation | One synthetic governed task outside the live queue. Negative cases block. One compliant case reaches the existing gates. | Runner evidence bound to the exact candidate SHA. | Caventra files, a batch, or auto-labeling unrelated issues. |
| 6 | This project's gitignored config | Set `governed-sdd` only after slices 1 through 5 pass and the founder authorizes that config change. | Doctor or audit output shows the profile on, and a missing spec blocks. | A code change hidden in the same edit. |

The issue 769 wording fix is a separate documentation correction. It is not a slice of this baseline.

## What this package does not do

- It does not mark `sdd-authority-0` or `sdd-authority-1` approved.
- It does not change `C:\development\stagemesh\main\.stagemesh\config.json`.
- It does not start a worker, label an issue ready, or merge an implementation change.
- It does not invoke Codex or alter provider authorization.
- It does not move the shared checkout or any existing task worktree.
