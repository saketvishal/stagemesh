# Versioning and stable installs

StageMesh uses annotated Git tags and published GitHub releases as reinstallable
package points. Version 0.5.0 introduces the operator activity API/dashboard and
label-scoped execution, alongside ownership, recovery and admission fixes since
0.4.0. See [the 0.5.0 release notes](releases/0.5.0.md) for scope and limits.

A version bump or an unpublished draft does not establish a published release.
After the `v0.5.0` release is published, verify the attached wheel against its
`SHA256SUMS` and provenance manifest. Alternatively, pin the annotated tag:

```powershell
python -m pip install "stagemesh @ git+https://github.com/saketvishal/stagemesh.git@v0.5.0"
```

Current lines:

- `0.5.0`: release line for operator visibility, scoped selection and reliability.
- `v0.4.0`: previous published release and retained rollback package.
- `v0.3.0`: focused-reading and relevant-test defaults.
- `v0.2.2` and `v0.2.1`: historical rollback releases.
- `v0.1.0`: historical fallback at the post-task-266 baseline.
- After publishing 0.5.0, start the next development change at `0.5.1.dev0`.

Release rule:

1. Align `pyproject.toml`, `src/stagemesh/__init__.py` and `build_backend.py`.
2. Independently review the exact candidate and pass required Windows/Linux and
   attribution checks before normal integration into protected `main`.
3. Verify the actual merged source, build assets from its clean checkout and
   record source provenance and checksums. Cut and push an annotated tag from
   that proven `main`, then publish the GitHub release with those assets.
4. Before starting new development, bump `main` to the next `.dev0` version.
5. Update a project-local runtime only with separate authorization while its
   owning processes, windows, executions and claims are idle. Publishing a
   release does not update installations, activate schedules or clear uncertain
   process ownership. Keep the previous verified wheel for rollback; inspect
   persisted-state compatibility before downgrading and preserve state backups.

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
