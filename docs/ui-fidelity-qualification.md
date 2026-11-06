# UI fidelity orchestration qualification

Issue: #505

Repository checkpoint: `da3db2274ae3452871df581ce7889249e091803d`

Qualification date: 2026-11-06

## Decision

**No-go for unattended Caventra UI fidelity ownership today. Conditional-go for a
bounded synthetic web experiment after the gaps below are reviewed and a separate
Caventra task is approved.**

StageMesh already has most of the control loop: bounded contracts, isolated
candidate checkouts, configured browser commands, exact-candidate validation,
artifact checksums, remediation findings, independent review, and fail-closed
integration. It does not currently prove the complete design-to-code loop required
by #505. In particular:

- the shipped Caventra profile does not run a browser or visual-comparison gate;
- StageMesh has no Figma- or Code Connect-specific approved-revision binding;
- the visual comparison algorithm, thresholds, deterministic rendering, and flaky
  rendering classification must be supplied by the project evaluator;
- the independent review prompt receives gate summaries, not the captured
  actual/reference/diff artifacts; and
- a configured evaluator runs in a disposable writable checkout. Candidate scope
  blocks an implementation commit from changing protected references, but StageMesh
  does not separately prove that the evaluator did not rewrite a reference at run
  time before returning PASS.

No live browser, Figma, Storybook, Maestro, native-device, or Caventra production
UI run was performed for this qualification. No Caventra source or runtime
configuration was changed. The conclusions below are repository evidence, not a
claim that an external toolchain is installed or operational.

## Capability audit

| Capability | Status | Repository evidence | Qualification boundary |
|---|---|---|---|
| Bound implementation scope and protected design files | Existing | `src/stagemesh/contracts.py` rejects changed files outside `allowed_files` and files matching `protected_files`; `docs/profiles/caventra.profile.json` protects the frontend Playwright configuration and `apps/web-v2/e2e/**`. | Protects committed candidate changes. It is not an evaluator-time read-only mount. |
| Frontend implementation guidance | Existing | `src/stagemesh/agent_skills.py` selects the pinned frontend-design skill; `docs/agent-skills.md` distinguishes installed, selected, attached, loaded, and applied. | Guidance is not proof of visual fidelity or correct application. |
| Project skills and provider-neutral capability opt-in | Existing | `skill_refs` and `capability_refs` are contract-bound; `docs/capabilities.md` documents disabled-by-default registration and stage/provider compatibility. | Registration or attachment is not invocation or behavioral acceptance. |
| Figma MCP / Code Connect design handoff | Not qualified | No Figma-specific adapter, revision schema, or acceptance fixture exists in this repository. The native registry route is bounded read-only stdio MCP for Codex or Claude. | Do not assume an arbitrary remote Figma MCP, OAuth flow, or plugin is supported. Exported, reviewed files can be ordinary protected project specs, but that is not a live Figma binding. |
| Structured design contract | Configurable | Contracts support `spec_refs`, acceptance criteria, invariants, file scope, required gates, and protected files. Provider prompts put project specs and approved UI ahead of skill procedures. | There is no first-class screen/state/token/design-revision model. A screenshot alone remains insufficient. |
| Storybook component states | Configurable, not present | A project-owned Storybook command can be an explicit contract gate or validation service command. | StageMesh does not discover Storybook or interpret stories. The shipped Caventra profile has no Storybook gate. |
| Playwright functional/browser gate | Existing and configurable | `src/stagemesh/validation_services.py` discovers conventional root npm E2E scripts and runs explicit `browser` services in an exact-candidate disposable checkout. | Caventra is a nested app and its shipped profile declares only install, Vitest, lint, and typecheck gates. An explicit `cwd` service is required. |
| Declared desktop/mobile-web state coverage | Existing and configurable | Browser requirements bind an id, target, state, and `WIDTHxHEIGHT` viewport. A fresh result manifest must report every declared id exactly once and all must PASS. | StageMesh trusts the configured evaluator to render and compare the state correctly. |
| Visual comparison and thresholds | Missing as a StageMesh primitive | StageMesh accepts evaluator PASS/FAIL and does not implement image comparison. | Pixel/perceptual algorithm, masks, fonts, animation controls, and thresholds belong to a reviewed project evaluator. |
| Actual/reference/diff artifact capture | Existing | `src/stagemesh/e2e_evidence.py` and `src/stagemesh/validation_services.py` constrain artifact count/size/path, copy refreshed artifacts, verify SHA-256, and bind candidate, baseline, contract, and service fingerprints. | Artifact content is not interpreted by StageMesh. A PASS command plus valid files is still evaluator-owned evidence. |
| Flaky-render distinction | Partial | Stable failure signatures correlate repeated failures and feed diagnosis. The evaluation runner deliberately performs no retries. | There is no visual-flake classifier. A project evaluator must render deterministically and explicitly report instability as failure. |
| Actionable remediation | Existing | Failed configured evaluations append `gate_failed` findings with bounded/redacted output and a stable failure signature; the normal coordinator remediation budget applies. | It can remediate a reported discrepancy, but cannot infer visual meaning from image bytes. |
| Independent evaluation identity | Partial | `require_independent` refuses a missing evaluator id or one matching the implementation provider. | This is a declared bounded identifier, not proof that a separate human/model inspected the images. |
| Independent code review before integration | Existing, with an evidence gap | Review is candidate/contract-bound and integration requires passing validation and review. The review prompt includes validation check and gate summaries. | The review prompt does not include the E2E artifact manifest or safe read-only artifact paths, so the reviewer cannot trace or inspect actual/reference/diff images. |
| Silent golden refresh protection | Partial | Caventra's profile protects E2E files and its hygiene gate checks visual-baseline changes. Scope validation rejects a candidate that changes protected references. | The evaluator checkout is writable and there is no before/after digest fence for declared reference files. |
| Shared-component cross-screen regression | Configurable, not demonstrated | A Playwright evaluator can declare requirements for multiple screens; omitted or failed ids fail closed. | No Caventra experiment proving the second-screen failure was run. |
| Native mobile / Maestro | Unsupported and not qualified | The shipped Caventra profile represents `apps/web-v2`; StageMesh evaluation kinds are `browser` and `api`, with no Maestro/native-device adapter or native-app evidence model. | Defer until a real native stack and device/emulator authority are separately established. Do not represent web viewport coverage as native-mobile evidence. |

## What the current trace can prove

For a configured browser service, StageMesh can bind and retain this chain:

1. task contract digest and recorded baseline SHA;
2. exact implementation candidate SHA and implementation provider;
3. validation-service fingerprint and per-service policy fingerprint;
4. executed gate status and declared requirement ids;
5. captured artifact names, sizes, SHA-256 digests, and isolated artifact root;
6. validation outcome, bounded remediation finding, and failure signature;
7. independent review outcome for the same candidate and contract; and
8. integration outcome for that exact candidate.

It cannot currently bind a Figma revision to step 1 or show that the reviewer in
step 7 inspected the artifacts from step 5. Therefore the acceptance chain in
#505 is incomplete even though its middle is implemented.

## Proposed bounded web toolchain

The smallest credible toolchain uses project-owned files and existing StageMesh
controls. It does not require a new scheduler or a provider-specific orchestration
path.

| Layer | Responsibility | Must not do |
|---|---|---|
| Approved design bundle | Version a design revision id, token values, component mappings, screen/state contract, responsive rules, and approved reference images. Record the source revision and SHA-256 values. | A screenshot must not be the only specification. No real legal data or proprietary architecture is sent to an external design service. |
| Change contract | Reference the approved bundle with `spec_refs`; limit implementation files; protect references, E2E specs, evaluator code, thresholds, masks, and gate configuration; require the fidelity service. | The implementation provider cannot update references, thresholds, masks, fixtures, or gate code. |
| Frontend provider | Implement only the allowed source/test changes using the selected frontend skill and project specs. | It does not approve its own output or change acceptance policy. |
| Storybook, if already supported by the project | Exercise shared components in named states and provide a fast component-level check. | It is not a substitute for routed-screen behavior or responsive browser coverage. |
| Playwright project evaluator | Start the synthetic app, render fixed states/viewports, disable motion/network variance, compare approved references, run functional assertions, and emit actual/reference/diff/trace artifacts plus the StageMesh result manifest. | It cannot update references or relax thresholds. An unstable render returns failure, not PASS. |
| StageMesh | Run the declared evaluator on the exact candidate, validate complete requirement coverage, capture bounded artifacts, feed failures into remediation, require independent review, and gate integration. | It does not infer design intent or silently install browser dependencies. |
| Independent reviewer | Inspect the patch, design contract, gate result, and read-only captured artifacts for the exact candidate. | It cannot treat a green status alone as visual approval. This artifact handoff is a current gap. |

Figma or Code Connect may be the upstream authoring source, but the experiment
must consume a reviewed, versioned export inside the project boundary. A live MCP
connection is optional and is not acceptance evidence. For native mobile, no tool
is selected: first prove that a native app exists and define an emulator/device,
fixture, screenshot, and cleanup contract. Only then evaluate Maestro under a
separate objective.

## Synthetic experiment specification

This experiment belongs in a separately approved Caventra task. It uses synthetic
records only and must not alter production UI routes or data.

### Approved inputs

- Design revision: `synthetic-workspace-v1`.
- Primary screen: a synthetic case-workspace summary.
- Dependent screen: a synthetic activity screen using the same status banner and
  primary action component.
- Reference bundle: structured screen/state JSON, tokens, component mapping, and
  approved PNG references, all committed under a protected test-fixture path.
- Fixed test data: invented names, dates, identifiers, and text with no legal
  matter content.
- Rendering controls: pinned browser and fonts, fixed locale/time zone/color
  scheme/device scale, reduced motion, no external network, and deterministic
  fixture responses.

### Required coverage

| Requirement id | Screen | State | Viewport | Expected checks |
|---|---|---|---|---|
| `workspace-ready-desktop` | workspace | ready | `1440x900` | approved visual plus visible primary action |
| `workspace-ready-mobile` | workspace | ready | `390x844` | approved responsive visual, no clipping/overflow |
| `workspace-empty-mobile` | workspace | empty | `390x844` | approved empty copy and next action |
| `workspace-error-desktop` | workspace | error | `1440x900` | approved error message and recovery action |
| `workspace-recovered-desktop` | workspace | recovery | `1440x900` | recovery interaction reaches approved ready state |
| `activity-shared-desktop` | activity | ready | `1440x900` | shared status banner and primary action remain approved |

An illustrative runtime declaration, not a change to the shipped Caventra profile:

```json
{
  "validation_services": [
    {
      "name": "synthetic-ui-fidelity",
      "kind": "browser",
      "cwd": "apps/web-v2",
      "command": ["npm.cmd", "run", "e2e:ui-fidelity", "--", "--project=chromium"],
      "timeout_seconds": 180,
      "artifacts": [
        "test-results/ui-fidelity/actual.png",
        "test-results/ui-fidelity/reference.png",
        "test-results/ui-fidelity/diff.png",
        "test-results/ui-fidelity/trace.zip"
      ],
      "evaluator": "playwright-ui-fidelity",
      "require_independent": true,
      "result_manifest": "test-results/ui-fidelity/stagemesh-results.json",
      "startup_command": ["npm.cmd", "run", "dev:test", "--", "--host", "127.0.0.1", "--port", "4173"],
      "health_command": ["node", "scripts/check-health.mjs", "http://127.0.0.1:4173/health"],
      "startup_timeout_seconds": 60,
      "requirements": [
        {"id": "workspace-ready-desktop", "kind": "browser", "target": "/synthetic/workspace", "state": "ready", "viewport": "1440x900"},
        {"id": "workspace-ready-mobile", "kind": "browser", "target": "/synthetic/workspace", "state": "ready", "viewport": "390x844"},
        {"id": "workspace-empty-mobile", "kind": "browser", "target": "/synthetic/workspace", "state": "empty", "viewport": "390x844"},
        {"id": "workspace-error-desktop", "kind": "browser", "target": "/synthetic/workspace", "state": "error", "viewport": "1440x900"},
        {"id": "workspace-recovered-desktop", "kind": "browser", "target": "/synthetic/workspace", "state": "recovery", "viewport": "1440x900"},
        {"id": "activity-shared-desktop", "kind": "browser", "target": "/synthetic/activity", "state": "ready", "viewport": "1440x900"}
      ]
    }
  ]
}
```

The evaluator must freshly write this bounded result shape using the fingerprint
provided in `STAGEMESH_E2E_POLICY_FINGERPRINT`:

```json
{
  "policy_fingerprint": "<environment value>",
  "checks": [
    {"id": "workspace-ready-desktop", "status": "PASS"},
    {"id": "workspace-ready-mobile", "status": "PASS"},
    {"id": "workspace-empty-mobile", "status": "PASS"},
    {"id": "workspace-error-desktop", "status": "PASS"},
    {"id": "workspace-recovered-desktop", "status": "PASS"},
    {"id": "activity-shared-desktop", "status": "PASS"}
  ]
}
```

StageMesh already rejects missing, duplicate, unknown, stale, or failed entries.
The evaluator must use an explicitly reviewed per-state threshold. A deterministic
synthetic reference should start at exact comparison; any non-zero threshold or
mask needs a documented rendering reason and separate approval.

### Required negative controls

1. **Mismatching design:** change a visible token, spacing rule, or primary-action
   placement only in an allowed implementation file. At least the applicable
   desktop and mobile requirements must fail and produce refreshed diff artifacts.
2. **Cross-screen regression:** change the shared banner/action component so the
   primary screen appears acceptable but the activity screen drifts. The
   `activity-shared-desktop` requirement must fail.
3. **Missing evidence:** omit a required result id or one of the declared artifacts.
   Validation must fail without entering independent review or integration.
4. **Candidate golden refresh:** attempt to commit a changed reference, threshold,
   mask, fixture, E2E spec, or evaluator. Contract scope must reject the candidate.
5. **Evaluator-time refresh:** make the evaluator rewrite its approved reference
   before comparison. This must fail before qualification can pass. Current
   StageMesh does not supply this proof, so the experiment is blocked on the
   mutation-fence follow-up below.
6. **Flaky rendering:** deliberately vary one render across two clean contexts. The
   evaluator must report an instability failure with no accepted PASS and no
   automatic reference update.

### Acceptance evidence

The experiment passes only when all positive requirements pass and every negative
control fails for the intended reason. The report must retain:

- approved design revision and reference SHA-256 values;
- task id, contract digest, baseline SHA, and exact candidate SHA;
- service and policy fingerprints;
- actual/reference/diff/trace artifact digests;
- per-requirement outcomes and functional assertions;
- remediation attempts and terminal outcome;
- independent reviewer identity and verdict for the same candidate; and
- proof that neither the candidate nor evaluator changed protected references or
  acceptance policy.

## Minimum follow-up work, deferred pending review

These are scoped ticket candidates, not authorization to implement or change
Caventra under #505.

1. **Reference mutation fence (StageMesh):** let an explicit browser service
   declare read-only reference inputs and acceptance-policy files. Hash them before
   and after evaluation in the disposable checkout and fail if any bytes, path
   type, or symlink target changes. Persist only bounded paths and digests.
2. **Review artifact handoff (StageMesh):** add a privacy-bounded validation
   evidence bundle for independent review containing requirement outcomes and
   read-only local artifact references/digests for the exact candidate. Keep raw
   targets, secrets, and unrestricted filesystem access out of the prompt.
3. **Synthetic Playwright qualification (Caventra, separate approval):** add the
   reviewed reference bundle, deterministic evaluator, state fixtures, and explicit
   runtime service configuration; execute all positive and negative controls. Do
   not touch production UI or real matter data.
4. **Native-stack decision (separate discovery):** verify whether Caventra has a
   native mobile target. If it does, define emulator/device isolation and assess
   Maestro. If not, close native-mobile qualification as not applicable rather
   than treating responsive web as equivalent.

## Go/no-go gate

StageMesh may be described as owning the UI feedback loop only after the two
StageMesh safety gaps are accepted, the separate synthetic Caventra experiment
passes on an exact candidate, the independent reviewer can inspect the captured
evidence, and hosted merge policy rejects the two negative regressions. Until
then, the accurate statement is: **StageMesh can orchestrate a configured
candidate-bound browser evaluator and remediate its failures, but unattended
Caventra design fidelity is not yet qualified.**
