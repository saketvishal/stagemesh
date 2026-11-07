# Validation component dependencies (bounded audit)

Integration timeout policy: a gate with no output is inconclusive. Preserve its candidate and diagnose the gate before another
implementation pass; an empty log does not establish an environment fault or code defect. This is stricter than the former single
speculative implementation retry. Structured collection/setup/service evidence retains its specific diagnosis, while evidence that
tests were running keeps the existing bounded remediation policy. Independent review of this policy change is required before release.

The immutable catalog covers exact roots for candidate contract binding, exact
integration evidence, planning, validation execution, review, the read-only
operator surface, capabilities and task sources. It is the single component/test
policy consumed by both task-level validation and hosted CI. It remains a bounded
selector rather than a call-graph inference engine: shared lifecycle, persistence,
schema, authorization, security and unknown roots deliberately require broad
validation. Use it with [focused validation](focused-validation.md) and the
approved task contract.

## Component map

| Component / source | Direct dependencies and contracts | Consumers / dependent impact |
| --- | --- | --- |
| Candidate contract binding: `src/stagemesh/contract_binding.py` | Frozen change-contract loading and canonical digest; task baseline/binding records; Git ancestry and inherited-candidate baseline rules; contract feasibility. | Validation, review, coordinator and integration all request the exact binding for a task/candidate. Recovery and completion integrity must reject missing, stale or mismatched bindings. |
| Exact integration evidence: `src/stagemesh/integration.py` | Candidate contract binding; exact validation/review evidence lookup; independent-review verification; Git attribution and integration execution ownership. | Coordinator completion, landing recovery and reliable delivery require durable exact-bound integration evidence. A merge or successful command alone is not completion evidence. |
| Planning: `src/stagemesh/validation_plan.py` | `contracts.py`: ChangeContract, GateCommand, executable versus declarative invariants. Derives classification, check names and escalation reasons; discovers declared browser scripts. | `Validator.validate` consumes the plan; `cli.command_validation_plan` displays it; `profile.py` imports broad-gate predicates. |
| Validation execution: `src/stagemesh/validation.py` | Planning; `focused_validation.py` discovers candidate tests; `contract_binding.contract_for_candidate` supplies the frozen contract/baseline; `contracts.evaluate_contract` executes gates. Store/domain, process identity, audit and remediation supply execution ownership and evidence/finding records. | Coordinator invokes Validator; Reviewer consumes PASSED validation summaries bound to task, candidate SHA and contract digest. A plan label alone is never execution evidence. |
| Review: `src/stagemesh/review.py` | The same bound contract and evaluator, with `run_gates=False`; Store supplies exact-bound validation evidence and implementer identity. Review adapter/pool provides an actual independent verdict. Domain, process identity, queue control, audit and remediation handle status, stop and finding records. | Coordinator consumes review evidence and `independent_review_verified`; integration relies on exact-bound evidence. Focused tests cannot replace independent review. |
| Read-only operator surface: `src/stagemesh/activity.py`, `src/stagemesh/activity_periods.py`, `src/stagemesh/operator.py`, `src/stagemesh/api_server.py`, and the exact packaged assets `src/stagemesh/ui/{index.html,app.js,app.css}` | Typed activity/period projections and bounded operator summaries read the workspace-local store. The loopback-only, bearer-authenticated API opens SQLite in query-only mode, redacts allowlisted output and serves exactly the three packaged assets. | `stagemesh activity`, operator reporting and `stagemesh serve` consume these projections. Browser behavior, API compatibility, packaging/launcher behavior, IANA timezone handling and Windows user-install acceptance are dependent contracts. Shared CLI, persistence/schema, coordinator lifecycle, claims, authorization, security, packaging metadata and installer implementation remain outside this narrow source boundary. |

The internal dependency chain is integration -> exact review/validation evidence ->
candidate contract binding, while review consumes validation evidence -> planning.
Review does not call the planner to select another suite. Contract evaluation and
persistence remain shared boundaries; their broad modules are not claimed as mapped
components. General provider implementation execution (`execution.py`) is outside
this audit. The operator surface reads shared storage and lifecycle state but does
not make those shared writers narrow: a change to persistence, schema, lifecycle,
claims, authorization or security still has unknown cross-cutting impact and
requires the complete regression.

## Direct and dependent test contracts

Each row names pytest targets relative to the repository root. Run
`python -m pytest -q <targets>` with the project interpreter. These are explicit
starting contracts for an affected behavior, not replacements for approved gates.
Validator binds exact mapped targets as additive gates while broader required
checks remain authoritative; it does not rewrite frozen contracts.

| Component | Direct checks and behavior | Dependent checks and boundary |
| --- | --- | --- |
| Candidate contract binding | `tests/test_candidate_binding_hardening.py`: ancestry, inherited baselines and exact candidate binding. `tests/test_contract_admission.py`: immutable binding and explicit repair. `tests/test_contract_fail_closed.py`: missing/malformed contract refusal. | `tests/test_candidate_isolation_integration.py`: replacement candidates cannot inherit stale integration evidence. `tests/test_completion_integrity.py`: completion rejects missing bound evidence. `tests/test_task_recovery.py`: repair/recovery preserves exact binding semantics. |
| Exact integration evidence | `tests/test_candidate_isolation_integration.py`: integration is candidate-isolated. `tests/test_integration_recovery.py`: interrupted/recovered integration ownership. `tests/test_review_reliability.py`: `_has_exact_bound_evidence` rejects stale digest/version/baseline and non-independent review. | `tests/test_completion_integrity.py`: DONE needs durable exact evidence. `tests/test_landing_recovery.py`: landing reconciliation retains binding/evidence identity. `tests/test_reliable_delivery_end_to_end.py`: delivery cannot bypass validation/review/integration evidence. |
| Planning | `tests/test_validation_plan.py`: classifications, broad/focused checks, browser discovery, required-gate execution; core changes reject narrower declarations. | `tests/test_contract_fail_closed.py`: plans without executable gates fail; declared docs cannot hide core changes. `tests/test_invariant_semantics.py`: prose remains a review obligation, executable invariants remain mandatory. `tests/test_stagemesh_validation_profile.py`: profile selection feeds the planner. |
| Validation execution | `tests/test_contract_fail_closed.py`: missing/malformed contracts, absent executable checks and contract drift. `tests/test_validation_plan.py`: real Validator execution retains a declared broad gate and records its failure. | `tests/test_invariant_semantics.py`: execution/review separation. `tests/test_candidate_binding_hardening.py`: candidate ancestry, scope and exact evidence. `tests/test_builtin_execution_ownership.py`: validation/review execution ownership. |
| Review | `tests/test_review_reliability.py`: complete, malformed and incomplete verdicts, candidate/contract binding. `tests/test_review_terminalization.py`: execution ends on pass, failure, exception or interruption. | `tests/test_autonomy_review.py`: stale SHA, independent reviewer and remediation require new evidence. `tests/test_invariant_semantics.py`: actual review of prose constraints. `tests/test_builtin_execution_ownership.py`: concurrent owners remain isolated. |
| Read-only operator surface | `tests/test_activity.py`, `tests/test_activity_cli.py`, `tests/test_activity_evidence.py`, `tests/test_activity_periods.py`, `tests/test_operator_api.py`, `tests/test_task_detail_api.py`, `tests/test_execution_detail_api.py`, `tests/test_operator_report.py`: typed projection, compatibility, filtering, redaction, evidence correlation and bounded detail contracts. | `tests/test_api_server.py`, `tests/test_operator_ui_contract.py`, `tests/test_ui_assets.py`, `tests/test_ui_browser_behavior.py`, `tests/test_ui_error_policy.py`, `tests/test_ui_launcher.py`, `tests/test_windows_user_installer.py`: loopback/auth/query-only policy, real-browser behavior, packaged assets, launcher behavior, Windows installer/timezone sensitivity and safe failure states. |

These contracts overlap deliberately: a shared test file can exercise more than
one boundary. Listing them does not claim they were all run for this slice.

## Machine-readable plan evidence

`ValidationPlan.to_dict()` exposes the audited map as three additive JSON arrays:

| Field | Meaning |
| --- | --- |
| `affected_components` | Stable names: `planning`, `validation_execution`, `review`. |
| `direct_test_targets` | Repository-relative pytest targets from the matching direct-check rows above. |
| `dependent_test_targets` | Repository-relative pytest targets from the matching dependent-check rows above. |
| `full_regression_required` | Conservative policy decision for the exact scope. |
| `full_regression_reasons` | Stable bounded rationale for either decision. A `false` decision still explains the applicable docs/config-only policy. |
| `component_policy_fingerprint` | SHA-256 identity of the exact audited roots, component names, platform needs and direct/dependent test targets. |

Each array is sorted and de-duplicated independently. A target can appear in both
test arrays when it has both roles across affected components. Matching uses only
the three exact source paths above, normalizing Windows separators and case in
the same way as existing planner classification. Client modules, helper names,
directories, glob patterns and test filenames do not infer component membership.
Concrete changed files take precedence; when none are supplied, matching uses
the contract's allowed scope, as classification already does.

Supporting `docs/**` and `tests/**` paths are neutral. If the actual scope (or the
allowed-scope fallback) contains any other path outside the three exact audited
source roots, all three inferred arrays are empty, including for mixed scope.
An empty array means no audited match or an incomplete map, not that validation is
unnecessary. Existing escalation reasons, including explicit unknown-impact reasons,
remain unchanged.
The three impact arrays do not claim transitive coverage. Validator may materialize
their exact direct and dependent targets as additive audited-component pytest gates;
it never uses them to remove or replace a declared gate. Required checks and
executable invariants keep their existing semantics, and `with_required_checks`
retains every evidence field. Validator evidence and CLI JSON expose the fields
through their existing plan serialization.

## Escalation and unknown impact

| Trigger | Reason to record / required response |
| --- | --- |
| Any of the five source roots changes | Core lifecycle/evidence boundary. The planner classifies all five as HIGH / CORE_LIFECYCLE_OR_SCHEMA_SECURITY, retains broad gates and executable invariants, and rejects a lower declared classification. |
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

All five exact task-contract planner source paths are in the core path list.
The machine-readable map never lowers classification or removes a gate; it can add
only the exact audited direct/dependent pytest targets. Provider names and
client-specific suites are not introduced. Authorization, independent review
requirements and candidate identity remain unchanged.

## Task-contract full-regression decision boundary

Every validation-core root remains a cross-cutting boundary and therefore still
requires full regression. Exact compatible operator, capability and task-source
roots may produce `false` only when every executable path is in the shared catalog
and all direct/dependent contracts are available. Operator roots are exclusive
from other fast-path families because their browser/installer contract differs.
An unmapped localized executable path, incompatible mix, executable provider/config
source, empty scope, public API impact, dependency impact or explicit escalation
requires full regression. Changes to either planner, the catalog, its acceptance
test or the runner force full regression. Declared gates are never removed by this
decision.

## Hosted-CI decision boundary

The `stagemesh.ci-validation/4` planner consumes exact changed paths from the same
catalog. Its fast path recognizes only 16 explicitly listed roots: the seven
operator roots above, eight skills/capability roots, and `task_sources.py`. Each
family owns immutable direct
and dependent pytest targets; documentation is neutral support. Ordering, case and
Windows separators normalize deterministically. Compatible capability/task-source
roots union their targets; the browser/installer-sensitive operator family does not
combine with another fast path. Support-only, empty, incompatible mixed, or unmapped scope selects `full`; so
do changes to the selector, workflow, profile, dependency manifests, schema,
persistence, coordinator lifecycle, claims, authorization, security, packaging,
shared CLI or public interfaces. It never infers safety from issue title, file
count, extension alone or arbitrary directory membership.

| Hosted family | Exact source/asset roots | Direct and dependent contract | Required OS |
| --- | --- | --- | --- |
| Operator | `activity.py`, `activity_periods.py`, `operator.py`, `api_server.py`, and the three packaged UI assets | Existing eight direct and seven browser/API/installer dependent tests | Windows + Linux |
| Skills/capabilities | `agent_skills.py`, `capabilities.py`, `capability_contracts.py`, `capability_discovery.py`, `capability_vendor.py`, `claude_plugin_capabilities.py`, `mcp_capabilities.py`, `provider_events.py` | Agent-skill, registry, MCP proxy/authorization, and provider-event direct tests plus provider acceptance/authorization/registry and run-window/recovery contracts | Windows + Linux |
| Task sources | `task_sources.py` | GitHub/local source, invariant, outbound lifecycle, progress-preservation, and zero-config direct tests plus admission diagnosis, objective, queue, selection, and publication contracts | Windows + Linux |
| Objective governance | `objective_governance.py` | Objective plan/challenge/revision/evaluation direct controls plus task-level planning and execution-gate compatibility checks | Windows + Linux |

An `operator` plan requires both protected Windows and Linux jobs, the real-browser
test and Windows installer/timezone acceptance. A `component` plan also requires
both operating systems for the currently audited skills/capability, task-source,
and objective-governance families. The planner writes versioned JSON with a selector-policy fingerprint;
a separate runner rejects symlinks, oversized/malformed plans, unsupported
contracts/modes/OS values, empty direct targets, missing/stale policy identity,
pytest options, traversal, shell text, and every target outside the immutable
component allowlist. It launches pytest with an argument list, never interpolated
shell text. A `docs` plan records portable
policy checks and explicitly sets `runtime_coverage=false`. A post-merge `smoke`
plan retains both operating systems, exact-range policy, bootstrap/import,
focused-policy tests, and executable/declarative invariants without repeating the
complete regression. Every mode retains the mandatory checks. The workflow prints
the versioned JSON plan. Its existing complete regression groups run for `full`;
the fixed operator command runs only for `operator` or `full`; the validated component
runner runs only for `component`. The protected job
names remain `windows` and `linux`.

Pull requests and merge groups use their explicit base/head identities with a
full-history checkout. A missing or invalid diff selects `full`; the mandatory
range policy separately fails closed when a candidate event lacks valid identity.
An ordinary push to `main` selects `smoke`; missing range identity or changes to
workflow, selection, dependency, schema, security, packaging, or other protected
boundaries select `full`. Scheduled and manual runs always select `full`, so
post-merge smoke evidence never replaces the authoritative recurring regression.

## Measured boundary and claim limit

The shared catalog records **21 exact source/asset roots**, **18 stable component
identities**, **four families**, and **50 distinct direct/dependent test files**.
Sixteen roots are eligible for a focused hosted path; the five validation-core
roots are evidence-only mappings that always retain full regression. The catalog
refactor removes the former separate local and hosted component maps, so one
fingerprint now identifies the roots and test contracts used by both planners.

Independent acceptance covers every fast root, deterministic compatible unions,
the exclusive operator boundary, cross-cutting full-only roots, unknown/mixed
scope, Windows path normalization, policy serialization, missing/stale fingerprint
rejection, target allowlisting and selector self-change escalation. These checks
prevent a new or renamed source path from silently acquiring narrow coverage.

The full coordinator call graph, dynamic adapter behavior, shared CLI call graph,
storage/schema internals and installer implementation remain intentionally broad.
They are not inferred as independently testable: their exact and name-based policy
entries select full regression. A future fast path for one of those areas requires
new dependency evidence and reviewed direct/dependent contracts; absence from the
catalog is never evidence that tests can be skipped. Hosted timings and results
remain exact-candidate evidence rather than a general speedup claim.
