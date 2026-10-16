# Versioning and stable installs

StageMesh uses git tags as reinstallable stable package points. The current known-good stable line is:

```powershell
python -m pip install "stagemesh @ git+https://github.com/saketvishal/stagemesh.git@v0.3.0"
```

Use that tag when a project needs the last proven operating version. For example, a product repository can refresh its project-local StageMesh runtime from the stable tag instead of from `main`.

Development on `main` moves to the next dev version after a stable tag is cut. That keeps experimental or in-progress StageMesh work from looking like the same package as the known-good release.

Current lines:

- `v0.3.0`: release point for generic focused-reading and relevant-test defaults. Stable publication requires the reviewed annotated tag and GitHub release.
- `v0.2.2`: previous stable rollback release for execution ownership, launch-failure diagnosis and accurate fallback progress.
- `v0.2.1`: previous stable rollback release, including candidate-environment and guarded delivery fixes.
- `v0.1.0`: historical fallback tag at the post-task-266 operating baseline.
- `main`: `0.3.0` release point; start the next development change at `0.3.1.dev0`. Existing clients stay on their installed version until explicitly updated while idle.

Release rule:

1. Cut an annotated tag from a clean, proven `main`.
2. Push the tag.
3. Before starting new development, bump `main` to the next `.dev0` version.
4. Refresh project-local runtimes from a tag when stability matters, or from `main` when testing new StageMesh features.

## Version 0.3.0: focused defaults in every repository

All implementation and review providers receive focused-reading and proportional
testing guidance without per-client setup. Conventional nearby Python and
Vitest/Jest checks are discovered using bounded path probes; validation reads the
exact candidate tree. Declared gates and invariants are always executed and
recorded, even when the advisory planner prefers a narrower check. Unknown
frameworks retain declared validation rather than guessing an unsafe command.
See `docs/focused-validation.md` for selection limits and package boundaries.

Clients receive this behavior only after installing the published 0.3.0 wheel or
annotated tag. Never overwrite a released 0.2.2 wheel or replace a live runtime.
Keep the published 0.2.2 package for rollback. No client configuration or existing
contract is rewritten by this release.

## Patch 0.2.2: execution ownership and failure handling

This patch contains the independently reviewed fixes in PR #364:

- Verify Windows provider ownership using the process handle already owned by
  StageMesh when querying the PID is unavailable. Unverified ownership still
  refuses termination and retains the claim for inspected recovery.
- Stop on established validation command-launch failures before spending coding
  attempts. Candidate import failures, missing data and test timeouts retain
  bounded remediation; no validation gate is skipped or weakened.
- Attribute progress and heartbeat lines to the actual execution actor, including
  fallback providers, and report terminal failure reasons.

Clients receive these changes only after installing **0.2.2** from its annotated
`v0.2.2` tag or the immutable wheel attached to that GitHub release. A local
untagged build or an unpublished release draft is not stable publication. Do not
replace an existing 0.2.1 package with different code under the same version.
Keep the published 0.2.1 wheel available for rollback, and wait for owning
StageMesh processes to exit before upgrading or rolling back a shared runtime.


## Patch 0.2.1: isolated gate dependencies

Version 0.2.1 was reviewed and integrated through PR #363. The annotated
`v0.2.1` tag and its GitHub release provide the stable wheel, source archive,
manifest and checksums. It remains available for client rollback after 0.2.2
is published. Do not describe an untagged local wheel as a
published stable release.

Every shipped fix must change the package version in `pyproject.toml`,
`src/stagemesh/__init__.py`, and `build_backend.py`. Use a patch version for
compatible fixes, a minor version for new compatible capabilities, and a major
version for incompatible public contracts. Package metadata, CLI runtime version,
and wheel version must agree. A client must install the released package to get
runtime fixes; never patch client-local installed source files or inject hooks.
An already running process does not reload a newly installed package. Install
only after StageMesh's owning processes have exited; do not terminate a client
run to upgrade it. A live provider's unresolved ownership is a separate safety
condition and is never cleared by upgrading.

### Candidate Python dependency environments

Clients may opt into `.stagemesh/validation-environments.json`:

```json
{
  "schema_version": 1,
  "python": {
    ".venv": "apps/api/.venv",
    "apps/api/.venv": "apps/api/.venv"
  }
}
```

Each key is a private `.venv` path in the temporary candidate checkout; its
value is a preinstalled `.venv` in the operator repository. This example also
supports Windows gate scripts that resolve a relative interpreter from the
checkout root before setting a backend child working directory. Other clients
configure their own paths; StageMesh contains no Caventra paths or application
imports. Without this file, existing behavior is unchanged.

Provisioning uses the source environment's own Python with site startup disabled,
including when StageMesh and the client use different Python minor versions.
The fresh environment references only the dependency directory with a plain path
entry; it does not execute the operator environment's editable-install `.pth`
files. Application source, tests, Git HEAD, and gate commands remain candidate-local.
Paths and symlink destinations must stay inside their respective repositories.
Existing candidate environments are never replaced. Tracked manifests and common
lockfiles must match, allowing Git line-ending normalization. A lockfile present
only as an untracked operator artifact is not a repository dependency declaration.
Dependency changes require an explicitly matching environment; no automatic
package installation, gate omission, result fabrication, or shared directory
junction is used. Candidate cleanup removes only its private environments.

Install a verified wheel through the existing standalone installer/package
workflow and verify `stagemesh runtime-info` reports the new version and source
commit. Retain the previous wheel for rollback. Publish the annotated stable tag
from reviewed main using the release rule above, then clients can pin that tag.
