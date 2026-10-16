# Versioning and stable installs

StageMesh uses git tags as reinstallable stable package points. The current known-good stable line is:

```powershell
python -m pip install "stagemesh @ git+https://github.com/saketvishal/stagemesh.git@v0.2.1"
```

Use that tag when a project needs the last proven operating version. For example, a product repository can refresh its project-local StageMesh runtime from the stable tag instead of from `main`.

Development on `main` moves to the next dev version after a stable tag is cut. That keeps experimental or in-progress StageMesh work from looking like the same package as the known-good release.

Current lines:

- `v0.2.1`: published stable release including candidate-environment and guarded delivery fixes.
- `v0.1.0`: historical fallback tag at the post-task-266 operating baseline.
- `main`: `0.2.2.dev0`, for continued StageMesh development.

Release rule:

1. Cut an annotated tag from a clean, proven `main`.
2. Push the tag.
3. Bump `main` to the next `.dev0` version.
4. Refresh project-local runtimes from a tag when stability matters, or from `main` when testing new StageMesh features.


## Patch 0.2.1: isolated gate dependencies

Version 0.2.1 was reviewed and integrated through PR #363. The annotated
`v0.2.1` tag and its GitHub release provide the stable wheel, source archive,
manifest and checksums. Install that release for client operation; `main` is
the next development line. Do not describe an untagged local wheel as a
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
