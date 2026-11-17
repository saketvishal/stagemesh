# Spec-driven development audit — issue 811

This is a read-only audit of existing StageMesh controls. It is not an activated architecture, not a new specification engine, and not founder approval. Closing or merging an issue does not make a document authoritative.

GitHub issue: https://github.com/saketvishal/stagemesh/issues/811 (OPEN at the time of this audit).

## Inspection identity

| Item | Value |
| --- | --- |
| Audit date | 2026-11-11 |
| Base | `origin/main` `55f2520367a3ef7d4b8ffc24c9fbebd3d62d5df8` (merge of pull request 813) |
| Worktree | `C:\development\stagemesh\worktrees\issue-811` |
| Branch | `docs/sdd-governance-audit-811` |
| Shared checkout | Left on `fix/latest-answer-terminal` at `10e1916265d2197f1b6c52807f7bedd0c2cb4204`. It was not switched. |
| Live project config | Read from `C:\development\stagemesh\main\.stagemesh\config.json`. That file is gitignored and is not copied into this worktree. |
| Production code | Not modified. |
| Adversarial runner | Not executed. Those cases are `NOT_VERIFIED`. |

The governed-profile modules named below are the same on this base as on the shared checkout. Pull request 813 did not edit them.

## How to reproduce the configuration reading

From the shared checkout, print policy keys only. Do not print provider commands, tokens, or other secret values.

```text
py -3 -c "import json; from pathlib import Path; d=json.loads(Path(r'C:\development\stagemesh\main\.stagemesh\config.json').read_text(encoding='utf-8-sig')); print(sorted(d)); print('planning_gate', d.get('planning_gate')); print('spec_governance', d.get('spec_governance')); print('runtime', d.get('runtime')); r=d.get('routing') or {}; print('routing', sorted(r), r.get('pools'), r.get('require_independent_review')); print('auto_label', (d.get('github') or {}).get('auto_label_unlabeled'))"
```

Observed on 2026-11-11:

- Top-level keys: `authorization`, `github`, `merging`, `providers`, `routing`, `task_sources`.
- `planning_gate` absent. `spec_governance` absent. `runtime` absent.
- `routing` contains only `pools`. IMPLEMENT and REVIEW are both `codex`, `claude`, `agy`. `require_independent_review` is absent, so the code default remains true.
- `github.auto_label_unlabeled` is true.
- `merging.mode` is `pr_only`, with required checks `linux`, `windows`, and `Attribution policy`.

Issue states read from GitHub, not inferred from documents:

| Issue | Title | State |
| --- | --- | --- |
| 325 | Require rigid PRD/spec planning before implementation | CLOSED 2026-11-04 |
| 338 | Spec Integrity Gate | CLOSED 2026-11-04 |
| 328 | Scoped prompts and diff enforcement | CLOSED 2026-10-13 |
| 326 | Adverse code review agents | CLOSED 2026-11-04 |
| 766 | Quality and validation gates | CLOSED 2026-11-08 |
| 767 | Per-task worktree isolation and quarantine | CLOSED 2026-11-08 |
| 768 | Provider-independent agent coding policy | CLOSED 2026-11-08 |
| 769 | Queue and qualify those gates | CLOSED 2026-11-08 |
| 811 | Certify enforced spec-driven development | OPEN |

## Capability matrix

Status meanings:

- `PRESENT`: the behavior exists and is on the normal path for this project, or the tracked policy file makes it apply when that path runs.
- `PARTIAL`: the code exists, but it is opt-in, unwired, or not in force for this project.
- `MISSING`: no product control does the required thing.
- `NOT_VERIFIED`: this audit did not re-execute the test or the live runner. Unit tests and older qualification notes are not a new end-to-end certificate.

| Capability | Issue | Implementation | Configured here | In the live lifecycle | This audit | Status |
| --- | --- | --- | --- | --- | --- | --- |
| Frozen planning spec and pre-implementation gate | 325 | `src/stagemesh/spec_planning.py` `evaluate_planning_gate`; called from `src/stagemesh/run_ready.py` `_enforce_planning_gate` and reported by `src/stagemesh/recovery.py` | No. Absent config means the gate is off (`config.py` `_planning_gate_policy`, default `require_approved_spec` false). | Off. Implementation can start without a planning spec. | Not re-run. `tests/test_spec_planning_gate.py` exists. | PARTIAL |
| Independent plan challenge | 325 | Same module. Distinct planner and challenger names are required when a challenge is present. `require_plan_challenge` defaults false and is rejected unless the spec gate is on. | No. | Off. | Not re-run. | PARTIAL |
| Executed planner and challenger receipts | 325 | `src/stagemesh/planning_execution.py` `ensure_executed_planning`. StageMesh writes a missing spec or challenge only after a configured role runs in a disposable baseline checkout. It does not overwrite an existing artifact. | No `planner_provider` or `challenger_provider`. | Off. Manual artifacts would still be accepted if the gate were enabled without those roles. | Not re-run. | PARTIAL |
| Human approval of the planning spec | 325 | `.stagemesh/planning/<task>.approval.json` must have `approved` true and the current spec digest. With a challenge, it must also bind the challenge digest (`spec_planning.py` around the approval check). | No, because the gate is off. | Off. | Not re-run. The checker does not record an approver identity. | PARTIAL |
| Specification inventory and structural integrity | 338 | `src/stagemesh/spec_integrity.py`: pinned `openspec` layout-v1 and declarative `custom-json` adapters, `spec-audit`, and `spec_governance_gate`. | No `spec_governance` section. | `spec-audit` runs only when invoked. Candidate validation calls `validate_prerequisite_manifest` from `contract_check.py` and `validation.py` only when the contract or files look spec-governed (`openspec/`, `specs/`, or `adapter.json`). Ordinary StageMesh task contracts do not take that path. | Not re-run. | PARTIAL |
| Conflicting active authority | 338 | Structural duplicate IDs, status conflicts, and historical-versus-active references are adapter findings. The document says semantic conflicts are review findings, not mathematical proof (`docs/spec-governance.md`). | No. | Not applied to this repository's architecture documents. | Not re-run. Two documents already disagree about issue 769; see contradictions. | PARTIAL |
| Change contracts and allowed files | 328 | `src/stagemesh/contracts.py`. Default `allowed_files` is `**` when a contract omits the field. | Per task. Gitignored contracts under the shared checkout are not part of this commit. | A contract blocks paths outside `allowed_files`, and protected or forbidden overlaps fail admission. A wildcard allow is still a wide grant unless `scope_mode` is set. | Not re-run. | PARTIAL |
| Atomic scope and diff quarantine | 328, 767 | Opt-in `scope_mode` `STRICT`, `BOUNDED`, or `EXPLORATORY`, with owner and revision. Out-of-scope diffs quarantine. Documented in `docs/atomic-scope-and-quarantine.md`. | Only when a task contract sets `scope_mode`. | Not a project-wide default. | Not re-run. `docs/development-readiness-qualification.md` records synthetic and hosted pass for the earlier qualification, which is not this audit. | PARTIAL |
| StageMesh task worktrees | 767 | `src/stagemesh/workspaces.py` uses `runtime.worktree_root`. The default in `config.py` `_runtime` is `<project>/.stagemesh/worktrees`. Broad roots, home, the checkout, and the project parent are rejected. | `runtime` is absent, so the default applies. Existing task worktrees under `C:\development\stagemesh\main\.stagemesh\worktrees` were left in place. | Yes for tasks the StageMesh runner creates. | The worktree list was read. Paths were not moved. | PRESENT for the runner |
| External agent worktrees | 811 workspace rule | No StageMesh admission check wraps an operator's own `git worktree add`. `autonomy/isolation.py` treats `stagemesh-candidate-*/checkout` under the process temp directory as owned by a run. `AppData\Local\Temp` is not in the shared-root denylist, so a configured `worktree_root` there is not rejected by the checks that reject `C:\Users` itself. | The requested development root is `C:\development\stagemesh`. This audit worktree follows that rule. | An agent can still create a source worktree in the Windows temp directory without going through StageMesh. | Observed historically for issue 804 and corrected outside this audit. Not re-tested here. | MISSING as a product block on external agents |
| Repository quality gate | 766 | Tracked `.stagemesh/quality-policy.json` (`policy_id` `stagemesh-repository-quality/1`). `quality_policy.py` loads it from the integration checkout and adds matching commit-tier gates. `scripts/quality_gate.py` is the direct gate. A candidate copy of the policy cannot authorize itself. | Yes, by the tracked file. | Wired when contract validation runs. | Not re-run. Hosted CI has a quality-gate step; this audit did not open a new CI run. | PRESENT |
| Provider-neutral coding constraints | 768 | Tracked `.stagemesh/agent-policy.json` (`policy_id` `stagemesh-default-coding-policy`). `agent_policy.py` reads the baseline copy, injects it through `providers.py`, and quarantines policy-file tampering. Intent heuristics are review signals, not proof. | Yes, by the tracked file. | Wired when the common implementation adapter builds a prompt. | Not re-run. | PRESENT |
| Independent and adverse review | 326 | `review.py` excludes the implementer when a review pool is used. `routing.require_independent_review` defaults true. Adverse policy defaults to hold when a reviewer is unavailable. | The live key is absent, so the default true stands. The same three provider names are listed for IMPLEMENT and REVIEW. | A different pool member can review. One usable provider cannot independently review itself. | Not re-run. Known product limit: on this base, `invariant_review_unavailable` is an infrastructure failure and is not by itself the durable external-dependency hold. That hold exists only on the unmerged issue 803 candidate. This audit did not change it. | PARTIAL |
| Exact candidate evidence and PR-only landing | delivery | `autonomy/merge_policy.py` checks evidence bound to the candidate. Live `merging.mode` is `pr_only`. | Yes. | Promotion stays PR-only. GitHub ruleset 23915766 was not inspected as a changed setting and was not modified. A workflow file is not proof of branch protection. | Not re-run. | PRESENT as policy; branch-protection enforcement `NOT_VERIFIED` |
| Objective-level acceptance | 811, parent 420 | `src/stagemesh/objective_governance.py` freezes plans, challenges, and evaluations. `docs/objective-governance.md` says the verdict does not treat child completion or component-test counts as achievement. | No objective artifacts were adopted for issue 811. | The module is not called from `coordinator.py`, `run_ready.py`, `cli.py`, or `validation.py`. Task `DONE` still means contract, validation, review, and integration. | Not re-run. The module's own document calls the qualification synthetic. | PARTIAL as code, MISSING on the delivery path |
| Named governed SDD profile | 811 | No `governed_sdd` profile or default-on switch was found. The pieces above stay independent opt-ins. | No. | No. | Not re-run. | MISSING |
| Prompt-proof enforcement | 811 | StageMesh gates apply only when work enters the runner. A coding agent that edits the checkout directly never reaches `evaluate_planning_gate`. | Not applicable. | Changing an agent prompt does not enable the dormant gates. | Observed by this session's own ability to add a worktree and a document without a frozen planning spec. | MISSING for agents outside the runner |

## Bypasses that exist today

1. This project's live config leaves `planning_gate` and `spec_governance` unset. The runner's planning check returns immediately when `require_approved_spec` is false.
2. `github.auto_label_unlabeled` is true. Initialization can classify an issue ready from an explicit contract or a deterministic auto-plan. That path does not require an approved planning spec while the planning gate is off. This audit did not run `stagemesh init`.
3. A contract without `scope_mode` may allow `**`. Out-of-scope enforcement is then only as narrow as that pattern.
4. Spec integrity does not inspect ordinary source changes. It starts when the contract or diff looks like an OpenSpec or custom-json authority change.
5. Planning approval is a local JSON flag and digest match. The gate does not require an approver identity, and StageMesh does not have a writer that proves a different human approved it. `docs/reliable-delivery.md` says executed roles must not approve their own output. The file check is weaker than that sentence.
6. Planner and challenger process independence is proven only when `planner_provider` and `challenger_provider` are configured and receipts match. Those keys are absent.
7. Objective failure cannot stop delivery. Passing the task contract can still reach `DONE` when the original objective would evaluate as not achieved.
8. Agents working outside `stagemesh run-window` can change files, create temp worktrees, and rewrite documents. The runner never sees those actions.
9. `runtime.allow_unsafe_worktree_root` can override the broad-root refusal. It is not set here. It remains a documented escape hatch.

## Document classification

Classes describe authority. `ACTIVE` means the repository currently presents the document as the description of that mechanism. It does not mean the founder approved a new governed profile.

| Document or record | Class | Why |
| --- | --- | --- |
| `README.md` lines 1–9 | ACTIVE | Current product description: provider-neutral control plane and the task lifecycle. The `PLAN` step is not backed by the default planning gate. |
| `docs/founder-hands-off.md` | ACTIVE | Canonical for that gate. The gate is explicitly `NOT MET` (0/10). |
| `docs/spec-governance.md` | ACTIVE | Current opt-in, read-only specification audit. It says semantic and founder-approval questions are not inferred. |
| `docs/reliable-delivery.md` planning section | ACTIVE | Current description of the opt-in planning challenge. The policy is disabled by default. |
| `docs/atomic-scope-and-quarantine.md` | ACTIVE | Current opt-in scope and quarantine behavior. |
| `docs/agent-coding-policy.md` | ACTIVE | Current coding-policy precedence. The tracked JSON is the baseline policy. |
| `docs/code-quality-gate.md` | ACTIVE | Current quality-gate description. The tracked JSON is the repository policy. |
| `docs/objective-governance.md` | ACTIVE as a limit | The mechanism exists and is explicitly not a completed objective demonstration. |
| `.stagemesh/agent-policy.json` | ACTIVE | Tracked effective default constraints. `approved_by` is `repository-owner`, which is a file field, not a new founder decision recorded by this audit. |
| `.stagemesh/quality-policy.json` | ACTIVE | Tracked repository quality policy. |
| Live `.stagemesh/config.json` | ACTIVE for this checkout | Effective operator configuration. Planning and spec governance are off. |
| `docs/development-readiness-qualification.md` | CONFLICTED | It records the issue 766–769 qualification and also says issue 769 remains open until a hosted proof. GitHub shows issue 769 CLOSED on 2026-11-08. The closure is not treated as founder approval of issue 811. |
| Closed issues 325, 326, 328, 338, 766, 767, 768, and 769 | HISTORICAL | They record shipped work. Closure is not approval of a default-on governed profile. |
| This file | PROPOSED | Audit record awaiting founder acceptance. It does not supersede the documents above. |
| Agent or model memory | Not a project record | It must not be the source of truth for StageMesh architecture. |

No separate ADR directory was found. Architecture decisions that are in force are the documents and tracked policy files above, plus the code defaults.

## Contradictions

1. `README.md` prints `PLAN -> IMPLEMENT -> VALIDATE -> REVIEW -> INTEGRATE -> DONE`. `config.py` says an absent planning gate means no pre-implementation gate. Both statements are in the tree. Only the config default is what this project runs.
2. `docs/development-readiness-qualification.md` still says issue 769 remains open. GitHub says it is closed. The qualification file also says a passing focused test is not hosted evidence.
3. `docs/objective-governance.md` can mark an objective `NOT_ACHIEVED` or `INCONCLUSIVE` after every child task is done. The task lifecycle can still mark the task `DONE` from contract, validation, review, and integration.
4. `docs/reliable-delivery.md` requires a human approval that executed planner and challenger roles cannot mint. `evaluate_planning_gate` accepts any local approval object with the matching digest and `approved: true`.
5. Several qualification sentences say controls are present. The live project config does not enable the planning or spec-governance opt-ins. Implemented is not the same as activated.
6. Issue 811 asks for a default-on governed profile for StageMesh's own nontrivial development. The code does not contain that profile. Adding one would be a new decision, not a documentation fix.

## Proposed authority baseline

Version `sdd-authority-0`. Owner: founder. Approval status of every item below: `PROPOSED`. Nothing in this section is activated.

The proposal reuses the current documents. It does not replace them and does not create a second specification system.

| ID | Proposed requirement | Source already in the repository | Status |
| --- | --- | --- | --- |
| SDD-AUTH-001 | StageMesh is a provider-neutral control plane. Executors are replaceable. The task lifecycle is plan, implement, validate, review, integrate, and done. | `README.md` | PROPOSED |
| SDD-AUTH-002 | Founder Hands-Off remains unmet until the documented 10-task streak exists. This audit does not count toward that streak. | `docs/founder-hands-off.md` | PROPOSED |
| SDD-AUTH-003 | For a project that explicitly adopts the governed profile, implementation dispatch requires an approved planning spec bound to the contract digest and baseline SHA. | `spec_planning.py`, `docs/reliable-delivery.md` | PROPOSED |
| SDD-AUTH-004 | When that profile requires a challenge, the challenger identity differs from the planner, and the verdict is accept before approval. Revise and needs-human-decision block. | `spec_planning.py` | PROPOSED |
| SDD-AUTH-005 | The implementer does not approve the spec or the challenge. Approval names an owner distinct from the planner and challenger. | Intended by `docs/reliable-delivery.md`; the current file check does not prove it | PROPOSED |
| SDD-AUTH-006 | Specification authority is an opt-in pinned adapter until the founder turns it on. Historical owners do not satisfy active dependencies. StageMesh does not infer founder approval from a merged pull request. | `docs/spec-governance.md` | PROPOSED |
| SDD-AUTH-007 | Governed code changes use an explicit contract: allowed files, non-goals, invariants, and acceptance checks. Wildcard repository write access is not the governed default. | `contracts.py`, `docs/atomic-scope-and-quarantine.md` | PROPOSED |
| SDD-AUTH-008 | StageMesh-created task worktrees stay under the project `.stagemesh/worktrees` root unless the founder approves another project-owned root. Existing runtime worktrees are not relocated by this audit. | `workspaces.py`, `config.py` `_runtime` | PROPOSED |
| SDD-AUTH-009 | Operator development worktrees for StageMesh issues stay under `C:\development\stagemesh\worktrees\issue-<id>`. Review and evidence siblings use the same parent. This is an operator rule. The product allocator is a separate control. | This audit's placement. Not a runtime change. | PROPOSED |
| SDD-AUTH-010 | Independent review is required. The reviewer is not the implementer. Unavailable independent review holds the task. | `config.py` default, `review.py` | PROPOSED |
| SDD-AUTH-011 | The tracked agent policy and quality policy at the integration baseline are the ones that bind a candidate. A candidate cannot relax them to pass. | `.stagemesh/agent-policy.json`, `.stagemesh/quality-policy.json` | PROPOSED |
| SDD-AUTH-012 | Landing stays PR-only unless an explicit merge policy says otherwise. Required hosted checks remain an external gate. | Live merging config | PROPOSED |
| SDD-AUTH-013 | Passing unit tests do not prove the original objective. Objective acceptance uses the existing objective-governance verdict when the founder attaches an objective. It is not connected yet. | `docs/objective-governance.md` | PROPOSED |
| SDD-AUTH-014 | A requirement change is a new immutable version. The previous spec remains. Stale digests block resume and promotion. | `spec_planning.py` stale-identity result, when the gate is on | PROPOSED |
| SDD-AUTH-015 | Legacy projects keep the current opt-in defaults until the founder approves a governed profile. There is no silent tightening in this audit. | `config.py` defaults | PROPOSED |

If the founder rejects or changes any row, that row stays `PROPOSED` or becomes `SUPERSEDED` only by a later versioned document in this repository. This file must not be edited in place to imitate approval.

## Proposed enforcement contract

This describes the existing integration points a later, approved change would use. It is not implemented here.

| Point | Existing component | Required evidence when a governed profile is on | Fail closed |
| --- | --- | --- | --- |
| Task admission | `run_ready.py` `_enforce_planning_gate` | Approved spec digest, contract digest, baseline SHA, and challenge digest when required | `RunReadyRefusal` `planning_gate` or `planning_execution` |
| Implementation dispatch | `planning_execution.py` and provider prompt binding | Matching planner and challenger receipts when roles are configured | Role errors before a provider edits the task worktree |
| Resume | `recovery.py` planning summary | Same artifacts and digests | Status stays blocked with a typed next action |
| Candidate validation | `contracts.py`, `validation.py`, `quality_policy.py`, `agent_policy.py` | Exact candidate SHA, scope result, baseline policy hash | Quarantine or validation error |
| Spec-bearing changes | `spec_integrity.py` through `contract_check.py` and `validation.py` | Pinned adapter and prerequisite manifest bound to the candidate | Structural errors block; semantic uncertainty stays a review finding |
| Independent review | `review.py` | PASS from a provider that is not the implementer, bound to the candidate | Missing or same-provider review does not promote |
| Promotion | `autonomy/merge_policy.py` and PR-only merging | Exact-SHA validation and review | PR is not merged by policy when evidence is missing |
| Objective acceptance | `objective_governance.py` | Not wired. A later spec must say when `NOT_ACHIEVED` or `INCONCLUSIVE` blocks `DONE`. | Do not claim this audit added that block |

An amendment creates a new planning-spec version and a new digest. The previous file remains the prior version. Implementation pauses until approval binds the new digest.

## Bounded remediation backlog

These are gaps this audit could verify. None is authorized for implementation.

| ID | Gap | Why existing code is not enough | Forbidden until a separate approved spec | Proof a later change would need |
| --- | --- | --- | --- | --- |
| SDD-GAP-001 | This project does not enable the existing planning gate or spec audit. | The functions already fail closed when enabled. The live config leaves them off. | Do not invent a second planner. Do not flip the live config in the same change as a code rewrite. | A founder-approved config decision, then one synthetic task that blocks with the gate on and proceeds only with a real approval. |
| SDD-GAP-002 | Approval has no accountable actor. | Digest match plus `approved: true` accepts a file the implementer can write. | Do not treat a new JSON flag as founder approval by itself. | A refusal when the approver is missing or is the planner, challenger, or implementer. |
| SDD-GAP-003 | Objective verdict is unwired. | Task `DONE` ignores `objective_governance.py`. | Do not mark issue 420 or Founder Hands-Off met. | One synthetic objective where unit tests pass and the objective verdict stays `NOT_ACHIEVED` or `INCONCLUSIVE`, and the runner does not deliver. |
| SDD-GAP-004 | No single governed profile. | Operators must know five opt-ins. | Do not change legacy default behavior without an explicit compatibility decision and a visible downgrade. | Profile on and profile off covered by the existing gate tests, not a new framework. |
| SDD-GAP-005 | External temp worktrees are outside the runner. | StageMesh already places its own task worktrees under `.stagemesh/worktrees`. | Do not retarget `runtime.worktree_root`. Do not move the worktrees already under the shared checkout. | A product rule only if the founder wants StageMesh, rather than the operator, to refuse temp roots. Agent instructions are not that product rule. |
| SDD-GAP-006 | `docs/development-readiness-qualification.md` disagrees with the closed state of issue 769. | The file is a historical qualification note. | Do not rewrite it into a new architecture. | A small documentation correction with its own review, or an explicit historical banner. |

## Adversarial acceptance

Not run. Issue 811 asks for a real runner canary after the governed profile is approved. Running it now would either pass for the wrong reason, because the gates are off, or require enabling policy that the founder has not approved.

| Case | Result of this audit |
| --- | --- |
| Code without an approved spec | `NOT_VERIFIED`. The code path blocks only when `require_approved_spec` is true. |
| Obsolete architecture decision followed | `NOT_VERIFIED`. No active-authority adapter is configured for this repository. |
| Two documents claim current authority | Observed as a documentation conflict for issue 769. The runner does not block on it. |
| Silent specification edit | `NOT_VERIFIED` on the runner. Planner scope rules exist for the planner role only. |
| File outside approved scope | `NOT_VERIFIED` on a live task. Contract and quarantine code exist. |
| Worktree outside the project root | The StageMesh default root is inside the project. An external `git worktree add` is not blocked by that default. This audit's own worktree is under `C:\development\stagemesh\worktrees`. |
| Agent approves its own specification | `NOT_VERIFIED`. The approval check has no actor comparison. |
| Resume with a stale spec digest | `NOT_VERIFIED`. The stale check exists and is off. |
| Unit tests pass while the objective fails | `NOT_VERIFIED` on the runner. The objective module is unwired, so the expected block does not exist yet. |
| A compliant change passes the gates | `NOT_VERIFIED`. No governed canary was started. |

## Completion

Issue 811 is not complete. This document is the current-state audit and the proposed authority baseline. It does not enable a profile, change runtime behavior, launch a task batch, relabel issues, or merge.
