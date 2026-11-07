# UI fidelity orchestration qualification

Issues: #505 qualification; #561 StageMesh implementation

Base repository checkpoint: `54131452c7128d8ae27d88dbb61930fb45f0d98c`

Qualification date: 2026-11-07

## Decision

**The provider-neutral StageMesh governance layer is qualified with a bounded,
hermetic six-state synthetic experiment. This is a go for governing an explicitly
configured project-owned visual evaluator, but remains a no-go for claiming that
StageMesh has visually validated or autonomously redesigned the real Caventra UI.**

StageMesh already has most of the control loop: bounded contracts, isolated
candidate checkouts, configured browser commands, exact-candidate validation,
artifact checksums, remediation findings, independent review, and fail-closed
integration. Issue #561 adds immutable visual-policy inputs, typed visual evidence,
comparison-result enforcement, and a safe independent-review handoff. It still does
not prove the complete Caventra design-to-code loop required by #505. In particular:

- the shipped Caventra profile does not run a browser or visual-comparison gate;
- StageMesh has no Figma- or Code Connect-specific approved-revision binding;
- the visual comparison algorithm and deterministic rendering remain project-owned;
- no approved Caventra reference bundle or visual service is configured; and
- no real Caventra state, responsive route, or shared-component regression has run
  through the new governance layer.

No live browser, Figma, Storybook, Maestro, native-device, or Caventra production
UI run was performed for this qualification. The evaluator is a deterministic
synthetic command, not Playwright. No Caventra source, runtime configuration, or
approved baseline was changed. The conclusions below are repository evidence, not
a claim that an external toolchain is installed or operational.

## Capability audit

| Capability | Status | Repository evidence | Qualification boundary |
|---|---|---|---|
| Bound implementation scope and protected design files | Existing | `src/stagemesh/contracts.py` rejects changed files outside `allowed_files` and files matching `protected_files`; `docs/profiles/caventra.profile.json` protects the frontend Playwright configuration and `apps/web-v2/e2e/**`. | Protects committed candidate changes. It is not an evaluator-time read-only mount. |
| Frontend implementation guidance | Existing | `src/stagemesh/agent_skills.py` selects the pinned frontend-design skill; `docs/agent-skills.md` distinguishes installed, selected, attached, loaded, and applied. | Guidance is not proof of visual fidelity or correct application. |
| Project skills and provider-neutral capability opt-in | Existing | `skill_refs` and `capability_refs` are contract-bound; `docs/capabilities.md` documents disabled-by-default registration and stage/provider compatibility. | Registration or attachment is not invocation or behavioral acceptance. |
| Figma MCP / Code Connect design handoff | Not qualified | No Figma-specific adapter, revision schema, or acceptance fixture exists in this repository. The native registry route is bounded read-only stdio MCP for Codex or Claude. | Do not assume an arbitrary remote Figma MCP, OAuth flow, or plugin is supported. Exported, reviewed files can be ordinary protected project specs, but that is not a live Figma binding. |
| Structured design contract | Existing, opt-in | Browser services can declare `visual_reference.revision`, immutable typed inputs, comparison policy, functional gates, and typed actual/reference/diff artifacts; ordinary contracts still bind specs, scope, and protected files. | The revision identifies a reviewed project bundle, not a live Figma API revision. A screenshot alone remains insufficient. |
| Storybook component states | Configurable, not present | A project-owned Storybook command can be an explicit contract gate or validation service command. | StageMesh does not discover Storybook or interpret stories. The shipped Caventra profile has no Storybook gate. |
| Playwright functional/browser gate | Existing and configurable | `src/stagemesh/validation_services.py` discovers conventional root npm E2E scripts and runs explicit `browser` services in an exact-candidate disposable checkout. | Caventra is a nested app and its shipped profile declares only install, Vitest, lint, and typecheck gates. An explicit `cwd` service is required. |
| Declared desktop/mobile-web state coverage | Existing and configurable | Browser requirements bind an id, target, state, and `WIDTHxHEIGHT` viewport. A fresh result manifest must report every declared id exactly once and all must PASS. | StageMesh trusts the configured evaluator to render and compare the state correctly. |
| Visual comparison and thresholds | Existing governance; project-owned comparator | StageMesh binds an algorithm identifier and maximum difference ratio, rejects a PASS whose measured ratio exceeds the approved threshold, and never performs image comparison itself. | Pixel/perceptual implementation, masks, fonts, and animation controls belong to the reviewed project evaluator. |
| Actual/reference/diff artifact capture | Existing, typed | `src/stagemesh/e2e_evidence.py`, `src/stagemesh/validation_services.py`, and `src/stagemesh/visual_reference.py` require all three roles for every declared state, constrain path/size, copy artifacts, verify SHA-256, and bind candidate, baseline, contract, service, and visual-policy fingerprints. | StageMesh validates identity and completeness, not image semantics. |
| Flaky-render distinction | Existing declared signal | The result must declare `stable: true` and meet the configured bounded `stability_runs`; unstable or insufficient runs are environment failures, while threshold mismatches are assertions. | The project evaluator must actually produce deterministic repeated renders; StageMesh does not inspect browser internals. |
| Actionable remediation | Existing | Failed configured evaluations append `gate_failed` findings with bounded/redacted output and a stable failure signature; the normal coordinator remediation budget applies. | It can remediate a reported discrepancy, but cannot infer visual meaning from image bytes. |
| Independent evaluation identity | Partial | `require_independent` refuses a missing evaluator id or one matching the implementation provider. | This is a declared bounded identifier, not proof that a separate human/model inspected the images. |
| Independent code review before integration | Existing with bounded artifact handoff | Review is candidate/contract-bound; before prompting, StageMesh revalidates captured artifact checksums and supplies only project-relative roots, typed paths, sizes, digests, requirement results, and visual-policy identity. | This makes evidence inspectable but does not prove a human or model interpreted the pixels correctly. |
| Silent golden refresh protection | Existing, opt-in | StageMesh hashes declared reference, fixture, tolerance, gate, and package inputs in the exact candidate, before and after evaluation; it rejects mutation, path/symlink escape, stale identity, and explicit update-golden modes. | The project must declare every acceptance-policy input; undeclared external tool bytes remain outside this proof. |
| Responsive, empty, error and recovery states | Demonstrated synthetically in StageMesh | `tests/test_visual_reference_governance.py` declares desktop ready/error/recovery, mobile ready/empty, and a dependent activity screen; every state requires typed actual/reference/diff evidence. | The evaluator emits synthetic bytes; it does not prove browser rendering fidelity. |
| Mismatching design and shared-component cross-screen regression | Demonstrated synthetically in StageMesh | The hermetic acceptance matrix rejects desktop/mobile drift and independently rejects drift on the dependent activity screen. | No Caventra screen was exercised. |
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

It can bind a reviewed project revision identifier and make step 5 artifacts
available to step 7 as checksum-revalidated, content-free references. The accepted
hermetic matrix proves this StageMesh governance chain. It cannot prove that the
identifier came from a live Figma service, that a browser rendered Caventra, or that
the reviewer correctly interpreted the pixels.

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
| Independent reviewer | Inspect the patch, design contract, gate result, and checksum-revalidated read-only captured artifacts for the exact candidate. | It cannot treat a green status alone as visual approval or change policy inputs. |

Figma or Code Connect may be the upstream authoring source, but the experiment
must consume a reviewed, versioned export inside the project boundary. A live MCP
connection is optional and is not acceptance evidence. For native mobile, no tool
is selected: first prove that a native app exists and define an emulator/device,
fixture, screenshot, and cleanup contract. Only then evaluate Maestro under a
separate objective.

## Synthetic experiment specification

This experiment is implemented as hermetic StageMesh acceptance coverage using
synthetic records only. It does not alter production UI routes or data.

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

An illustrative runtime declaration, not a change to the shipped Caventra profile.
One requirement is shown compactly; the approved experiment must repeat the typed
artifact triplet for every requirement in the table above:

```json
{
  "validation_services": [
    {
      "name": "synthetic-ui-fidelity",
      "kind": "browser",
      "cwd": "apps/web-v2",
      "command": ["npm.cmd", "run", "e2e:ui-fidelity", "--", "--project=chromium"],
      "timeout_seconds": 180,
      "evaluator": "playwright-ui-fidelity",
      "require_independent": true,
      "result_manifest": "test-results/ui-fidelity/stagemesh-results.json",
      "startup_command": ["npm.cmd", "run", "dev:test", "--", "--host", "127.0.0.1", "--port", "4173"],
      "health_command": ["node", "scripts/check-health.mjs", "http://127.0.0.1:4173/health"],
      "startup_timeout_seconds": 60,
      "requirements": [
        {"id": "workspace-ready-desktop", "kind": "browser", "target": "/synthetic/workspace", "state": "ready", "viewport": "1440x900"}
      ],
      "visual_reference": {
        "revision": "synthetic-workspace-v1",
        "inputs": [
          {"path": "e2e/references/workspace-ready-desktop.png", "role": "reference"},
          {"path": "e2e/fixtures/synthetic-state.json", "role": "fixture"},
          {"path": "e2e/visual-thresholds.json", "role": "tolerance"},
          {"path": "e2e/ui-fidelity.spec.ts", "role": "gate"},
          {"path": "package.json", "role": "package"}
        ],
        "artifacts": [
          {"requirement_id": "workspace-ready-desktop", "role": "actual", "path": "test-results/ui-fidelity/workspace-ready-desktop.actual.png", "content_type": "image/png"},
          {"requirement_id": "workspace-ready-desktop", "role": "reference", "path": "e2e/references/workspace-ready-desktop.png", "content_type": "image/png"},
          {"requirement_id": "workspace-ready-desktop", "role": "diff", "path": "test-results/ui-fidelity/workspace-ready-desktop.diff.png", "content_type": "image/png"}
        ],
        "comparison": {
          "algorithm": "playwright-pixel-ratio-v1",
          "max_difference_ratio": 0.01,
          "stability_runs": 2
        },
        "functional_gates": ["synthetic-ui-functional"]
      }
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
    {
      "id": "workspace-ready-desktop",
      "status": "PASS",
      "difference_ratio": 0.0,
      "stable": true,
      "render_count": 2
    }
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
   before comparison. The pre/post protected-input digest fence must fail and retain
   an actionable environment finding; no PASS evidence may be accepted.
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

## Remaining follow-up work

No further StageMesh core change is required for the bounded synthetic qualification.
The following work remains outside this authorization and is not kept open under
#505:

1. **Live project-owned browser qualification (separate approval):** configure a
   real deterministic Playwright evaluator and approved reference bundle in the
   target project, then run it against an exact candidate. Do not touch production
   UI, refresh an approved baseline, or use real matter data without separate scope.
2. **Native-stack decision (separate discovery):** verify whether Caventra has a
   native mobile target. If it does, define emulator/device isolation and assess
   Maestro. If not, close native-mobile qualification as not applicable rather
   than treating responsive web as equivalent.

## Go/no-go gate

The accurate statement is: **StageMesh can govern a configured candidate-bound
visual evaluator, preserve immutable typed evidence, reject missing or silently
refreshed evidence and visual regressions, return failures to remediation, and bind
independent review to the same candidate. The six-state hermetic qualification is
synthetic; unattended real-Caventra design fidelity remains not run and unproven.**
