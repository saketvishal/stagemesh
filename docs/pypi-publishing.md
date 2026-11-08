# PyPI publishing

StageMesh publishes the `stagemesh` distribution from an explicit, non-prerelease GitHub release. The release workflow checks out the
exact tag, builds one wheel, verifies its stable version and clean Git provenance, installs it into a fresh environment, and proves that
the installed `stagemesh` command initializes a new project without provider calls. The verified artifact is passed unchanged to a
separate `pypi` environment job.

Publishing uses PyPI Trusted Publishing with GitHub OIDC. No long-lived PyPI token belongs in GitHub. The one-time PyPI owner action is
to add this pending trusted publisher before the first release:

| Field | Value |
| --- | --- |
| PyPI project name | `stagemesh` |
| GitHub owner | `saketvishal` |
| GitHub repository | `stagemesh` |
| Workflow | `publish-pypi.yml` |
| Environment | `pypi` |

The package name currently has no PyPI project. A pending publisher binds the expected first-publish identity, but PyPI explicitly does
not create the project or reserve its name until the first upload. Recheck availability immediately before release and publish promptly
after configuration. Configure the publisher only from the authenticated `saketvishal` PyPI account; StageMesh never asks for or stores
that account password or recovery codes.

Release order remains fail closed:

1. Merge the release candidate through protected `main` after its required Windows, Linux, and attribution checks pass.
2. From the verified merged commit, create and push the matching annotated tag (for example `v0.6.0`).
3. Publish a non-prerelease GitHub release for that exact tag. This explicit action triggers the PyPI workflow.
4. Require the `pypi` GitHub environment for the publish job. The workflow refuses mismatched tags, dirty provenance, multiple or
   unexpected distribution files, unsafe wheel members, installation failure, a missing console entry point, or failed new-project init.
5. After the workflow succeeds, independently install from the public index in a fresh environment:

   ```powershell
   python -m pip install --no-cache-dir stagemesh==0.6.0
   stagemesh --version
   stagemesh init
   ```

PyPI distributions are immutable. Never reuse a published version or enable `skip-existing`; a correction requires a new version.
