# Fail-closed code quality gate

StageMesh has one repository policy at `.stagemesh/quality-policy.json`. Managed repositories may opt in with the same schema and
their own language-appropriate direct commands. No Python tool is imposed when a repository has no policy.

## Enforcement layers

| Control | Current state | Authority |
|---|---|---|
| Exact candidate and baseline | Present | Bound task contract and validation evidence |
| Allowed, forbidden, protected and dependency scope | Present | Deterministic contract evaluation |
| Targeted and broader tests | Present | Contract gates, focused selection and hosted CI plan |
| Independent adverse review | Present | Exact-candidate review policy |
| Ruff unused/error/Bugbear checks | Enforced for changed Python files | `scripts/quality_gate.py` |
| McCabe complexity | Enforced at maximum 12 for new Python files | Ruff `C90`; legacy complexity is the migration baseline |
| Strict Mypy | Enforced for policy modules and newly added production Python | Explicit `strict_modules` migration set plus added source files |
| Type escape hatches | Enforced for newly added Python | `Any`, `cast`, and `type: ignore` require an audited boundary rather than silent use |
| Dead-code supplement | Enforced for newly added production Python | Vulture at 100% confidence |
| Local hooks | Fast feedback only | Pre-commit changed-file gate; pre-push commit gate and attribution check |
| Hosted enforcement | Required workflow step on Windows and Linux | `Fail-closed repository quality gate` |
| Gate configuration ownership | Declared, externally enforced | CODEOWNERS; branch protection must require Code Owner approval |

The migration boundary is deliberate. The existing tree contains legacy complexity and typing debt, so this change does not claim
that the entire repository already passes strict Mypy, Bugbear, or McCabe checks. New source files are held to the stronger policy;
Mypy follows neither imported legacy modules nor their transitive debt during this target-bounded check. The explicit
`strict_modules` list is expanded as legacy modules are repaired. Lowering thresholds, removing required lint/type
categories, using unsafe working directories, or supplying shell command strings makes the policy unreadable and validation fails.

## Trusted policy and evidence

StageMesh loads the quality policy from the integration checkout before it creates candidate gate commands. The policy hash is
placed in each command's environment and therefore in the existing complete-input fingerprint. The candidate runner verifies that
hash. A candidate that edits its copy of the policy cannot use the edited policy to validate itself: the hash differs and the gate
fails. Missing commands, missing tools, crashes, timeouts, skipped commands, non-zero results, and stale policy hashes are failures.

Validation evidence records the policy ID/hash, exact candidate and base SHAs, applied gate names, commands, executable resolution,
durations, bounded output and results. Prior passes are reusable only under the existing identical-tree, contract, command,
environment, executable, platform and policy fingerprint. Hooks are bypassable with `--no-verify`; they are never promotion proof.
CI and exact-candidate StageMesh validation remain authoritative.

## Scope and review boundary

Workflow, gate, policy, and dependency-manifest changes require an exact `allowed_files` entry in an explicit task contract. A broad
glob is not authorization for those boundaries. Existing deterministic scope checks reject other unrelated files and unrequested
dependency changes. Independent review is instructed to reject unnecessary complexity, broad rewrites, generic abstractions and
scope creep.

Static analysis cannot prove developer intent, useful test coverage, API appropriateness, absence of all dead code, or that an
abstraction is warranted. Mypy strict also does not by itself prohibit every `Any`, cast, ignore, or untyped third-party boundary.
Those limits remain explicit review obligations; ambiguous scope is not auto-approved.

## Commands

- Fast staged feedback: `python scripts/quality_gate.py --mode fast`
- Commit-bound local gate: `python scripts/quality_gate.py --mode commit`
- Authoritative CI gate: `python scripts/quality_gate.py --mode ci --base <full-sha> --head <full-sha>`
- Hook installation: `python scripts/install_attribution_hooks.py`

The CI command emits a JSON report when `--report` is supplied. A PASS is a mechanical repository gate, not proof that behavior is
correct; behavioral tests and independent review are still required.
