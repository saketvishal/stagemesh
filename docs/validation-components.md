# Validation component dependencies (bounded audit)

Integration timeout policy: a gate with no output is inconclusive. Preserve its candidate and diagnose the gate before another
implementation pass; an empty log does not establish an environment fault or code defect. This is stricter than the former single
speculative implementation retry. Structured collection/setup/service evidence retains its specific diagnosis, while evidence that
tests were running keeps the existing bounded remediation policy. Independent review of this policy change is required before release.

This explicit map covers three StageMesh components: planning, validation execution,
and review. It is a bounded contribution to #71; **#71 remains open**. It is a review
aid, not a global test selector or proof of complete coverage. Use it with
[focused validation](focused-validation.md) and the approved task contract.

## Component map

| Component / source | Direct dependencies and contracts | Consumers / dependent impact |
| --- | --- | --- |
| Planning: `src/stagemesh/validation_plan.py` | `contracts.py`: ChangeContract, GateCommand, executable versus declarative invariants. Derives classification, check names and escalation reasons; discovers declared browser scripts. | `Validator.validate` consumes the plan; `cli.command_validation_plan` displays it; `profile.py` imports broad-gate predicates. |
| Validation execution: `src/stagemesh/validation.py` | Planning; `focused_validation.py` discovers candidate tests; `contract_binding.contract_for_candidate` supplies the frozen contract/baseline; `contracts.evaluate_contract` executes gates. Store/domain, process identity, audit and remediation supply execution ownership and evidence/finding records. | Coordinator invokes Validator; Reviewer consumes PASSED validation summaries bound to task, candidate SHA and contract digest. A plan label alone is never execution evidence. |
| Review: `src/stagemesh/review.py` | The same bound contract and evaluator, with `run_gates=False`; Store supplies exact-bound validation evidence and implementer identity. Review adapter/pool provides an actual independent verdict. Domain, process identity, queue control, audit and remediation handle status, stop and finding records. | Coordinator consumes review evidence and `independent_review_verified`; integration relies on exact-bound evidence. Focused tests cannot replace independent review. |

The internal dependency chain is review -> validation evidence -> planning. Review
consumes validation evidence rather than calling the planner to select another
suite. Contract binding and evaluation are shared boundaries. General provider
implementation execution (`execution.py`) is outside this audit.

## Direct and dependent test contracts

Each row names pytest targets relative to the repository root. Run
`python -m pytest -q <targets>` with the project interpreter. These are explicit
starting contracts for an affected behavior, not replacements for approved gates.
A future task must bind relevant targets and broader required checks before
execution; this document does not change frozen contracts.

| Component | Direct checks and behavior | Dependent checks and boundary |
| --- | --- | --- |
| Planning | `tests/test_validation_plan.py`: classifications, broad/focused checks, browser discovery, required-gate execution; core changes reject narrower declarations. | `tests/test_contract_fail_closed.py`: plans without executable gates fail; declared docs cannot hide core changes. `tests/test_invariant_semantics.py`: prose remains a review obligation, executable invariants remain mandatory. `tests/test_stagemesh_validation_profile.py`: profile selection feeds the planner. |
| Validation execution | `tests/test_contract_fail_closed.py`: missing/malformed contracts, absent executable checks and contract drift. `tests/test_validation_plan.py`: real Validator execution retains a declared broad gate and records its failure. | `tests/test_invariant_semantics.py`: execution/review separation. `tests/test_candidate_binding_hardening.py`: candidate ancestry, scope and exact evidence. `tests/test_builtin_execution_ownership.py`: validation/review execution ownership. |
| Review | `tests/test_review_reliability.py`: complete, malformed and incomplete verdicts, candidate/contract binding. `tests/test_review_terminalization.py`: execution ends on pass, failure, exception or interruption. | `tests/test_autonomy_review.py`: stale SHA, independent reviewer and remediation require new evidence. `tests/test_invariant_semantics.py`: actual review of prose constraints. `tests/test_builtin_execution_ownership.py`: concurrent owners remain isolated. |

These contracts overlap deliberately: a shared test file can exercise more than
one boundary. Listing them does not claim they were all run for this slice.

## Escalation and unknown impact

| Trigger | Reason to record / required response |
| --- | --- |
| Any of the three source roots changes | Core lifecycle/evidence boundary. The existing planner classifies all three as HIGH / CORE_LIFECYCLE_OR_SCHEMA_SECURITY, retains broad gates and executable invariants, and rejects a lower declared classification. |
| Shared contract, binding, persistence, schema, authorization or evidence semantics change | Cross-cutting impact: inspect affected callers and retain broader approved validation. This map cannot establish a narrow boundary for those changes. |
| Public API or dependency manifest changes | Keep existing planner escalation and required checks. Component membership does not waive these concerns. |
| Unmapped source, ambiguous scope, uninspected caller, dynamic provider edge, or unavailable candidate diff | Unknown impact: record the concrete uncertainty in the existing `validation_escalation_reasons` contract field and retain broader validation. Absence from this map is not evidence of isolation. |

The map performs no runtime inference for unknown paths. An explicit unknown-impact
reason makes `planned_contract` retain broad gates and executable invariants;
`with_required_checks` preserves all declared executable gates regardless of
classification. Validator evaluates the bound contract, not a reduced contract
manufactured from this table. No test commands are invented for unknown impact.
If broader coverage is missing from a frozen contract, use the existing approved
contract revision/rebind process; do not silently narrow or rewrite it.

The only runtime code change in this slice adds the exact StageMesh `review.py`
path to the existing core path list, alongside planning and validation. It does
not match arbitrary client review modules or similarly named helper files.
Provider names and client-specific suites are not introduced. Authorization,
independent review requirements and candidate identity remain unchanged.

## Measured boundary and follow-up

This audit records **3 component roots**, their immediate contract/evidence edges,
and **9 distinct test files** in the table. These counts measure the documented
slice, not transitive dependency coverage, coverage percentage or speedup.
The added planner regressions exercise all three roots with four declaration
states, allowed-scope fallback, concrete changed files and Windows separators;
they also cover explicit unknown escalation and unrelated client/helper paths.

Still unmeasured: the full coordinator/integration call graph, dynamic adapter
behavior, CLI end-to-end behavior, all shared storage/schema dependencies, and
whether these starting test contracts cover every possible change in each root.
No repository-wide audit, global heuristic selector or source refactor is included.

Follow-up for #71:

1. Review these edges and test contracts against a concrete future change; record
   missing dependencies and unresolved impact before proposing narrower gates.
2. Audit one additional boundary under separately approved scope, especially
   contract binding/evidence consumers before claiming transitive coverage.
3. Measure gate durations and failures for exact candidates, then propose reviewed
   focused contracts only where evidence establishes a safe boundary.
4. Keep #71 open until its remaining scope is separately delivered. StageMesh owns
   candidate creation, independent review and PR/integration. This worktree change
   neither upgrades the stable runtime nor supplies landing evidence.
