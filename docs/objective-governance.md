# Objective governance

Parent objective: #420. Bounded implementation children: #738–#743.

## Current status

StageMesh has a provider-neutral policy foundation for governing an objective above its individual task plans. It freezes the original objective, measurable success criteria, child-task dependency DAG, independent challenge, explicit revision reconciliation, and independent outcome evaluation as immutable local artifacts.

This is **synthetically qualified policy**, not a completed #420 Gate 1 demonstration. The canonical Founder Hands-Off Gate 0 remains **NOT MET** because the clean real-provider streak remains 0/10. Nothing in this module starts a provider, installs a schedule, approves a plan, proves an evaluator is independent in the real world, or establishes that an objective was achieved.

## Artifact chain

All artifacts use canonical bounded JSON and retain a full SHA-256 digest. Bounded filename tokens keep deep Windows workspaces usable; the full digest remains inside the artifact and digest-only audit receipt.

1. **Objective plan**
   - preserves a stable objective id, original objective text, plan revision, non-goals, stable success-criterion ids and a dependency DAG;
   - requires every child task to link to at least one success criterion and every criterion to be covered by a child;
   - rejects duplicate ids, unknown dependencies, cycles, unbounded text/items and unknown fields;
   - revision 1 cannot claim history; later revisions must bind the exact previous plan and challenge.
2. **Independent challenge**
   - binds the exact objective, plan revision and plan digest;
   - requires distinct declared planner and challenger identities;
   - retains stable typed findings and `accept | revise | needs-human-decision`;
   - cannot authorize a later plan after its bound plan changes.
3. **Revision reconciliation**
   - increments by exactly one and preserves the original objective and every existing success criterion;
   - records one `adopted | rejected-with-rationale` response for every challenge finding;
   - rejects missing, duplicate or invented responses, so dissent cannot silently disappear.
4. **Outcome evaluation**
   - binds the exact plan and its accepted challenge, exact candidate id, runner identity and implementation identities;
   - requires a distinct declared evaluator and exactly one `PASS | FAIL | UNKNOWN` result for every original criterion, each with evidence references;
   - derives the verdict rather than accepting a self-declared result: any failure is `NOT_ACHIEVED`, otherwise any unknown is `INCONCLUSIVE`, otherwise `ACHIEVED`;
   - preserves bounded negative findings. Child completion and component-test counts are not inputs to the verdict.

The declared role identities are validated data. They do not prove that separate provider processes ran. Real process independence still requires execution-bound receipts and authorization comparable to the existing task-level planning execution boundary.

## Isolation and audit behavior

Artifacts are create-only under a project-owned `.stagemesh/objectives/<bounded-objective-token>/` directory:

- `plans/<revision>-<digest-token>.json`
- `challenges/<plan-revision>-<digest-token>.json`
- `evaluations/<plan-revision>-<digest-token>.json`

Unsafe objective ids, path escape and symlink traversal fail closed. Saving the same artifact is idempotent; another artifact cannot replace the same plan revision. Loaders recompute identities, reject filename/content mismatch and reconstruct the full append-only revision/challenge chain after restart.

Durable audit events are `objective.plan_recorded`, `objective.challenge_recorded` and `objective.evaluation_recorded`. Receipts retain only objective id, revision, artifact kind, full digest and project-relative path. Objective text, criteria, findings and evidence details stay out of the audit payload.

## Hermetic qualification

`tests/test_objective_governance.py` uses synthetic data and local SQLite only. Its end-to-end control:

1. freezes revision 1 with a deliberately omitted empty-state requirement;
2. records an independent `revise` challenge that identifies the planted omission;
3. freezes revision 2 with an explicit response, new stable criterion and dependent verifier task;
4. records a distinct accepted challenge for revision 2;
5. marks every child task done but records the new criterion as `UNKNOWN`;
6. proves the objective verdict remains `INCONCLUSIVE`;
7. closes and reopens the store, reloads the complete immutable lineage, and proves an unrelated objective did not change.

Negative controls cover self-review, non-accepted challenges, stale hashes, dropped findings, rewritten original criteria, cycles, missing or extra evaluation criteria, claimed verdicts, in-memory-only dependencies, tampering, duplicate revisions, unsafe ids, long Windows paths and symlink escape.

## Remaining #420 proof

This batch does not establish any of the following:

- the 10-task real-provider Founder Hands-Off Gate 0 streak;
- an executed objective planner/challenger/evaluator with process-bound independent identities;
- the complete Gate 1 synthetic engineering demonstration through real implementation, interruption/restart, hosted delivery and objective evaluation;
- superiority over a competent direct-agent/checklist baseline;
- Gate 2 adaptive research, a Caventra research result, legal correctness or professional signoff.

After Gate 0 is genuinely satisfied or separately waived by an explicit owner decision, the smallest next experiment is to connect these artifacts to authorized executed objective roles and run one unfamiliar synthetic engineering objective. A negative or unknown result must remain visible; it must not create an automatic feature backlog or Gate 1 success claim.
