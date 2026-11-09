# PyPI publishing

StageMesh publishes the `stagemesh` distribution from an explicit, non-prerelease GitHub release. The release workflow checks out the
exact tag, builds one wheel, verifies its stable version and clean Git provenance, installs it into a fresh environment, and proves that
the installed `stagemesh` command creates a new project's safe setup without provider calls and truthfully reports that an empty
project still needs validation and provider configuration. The verified artifact is passed unchanged to a
separate `pypi` environment job.

Bundled skill resources retain their logical source names, upstream URLs, and blob-integrity checks, but the wheel stores their physical
members under a compact logical-path-derived layout. This avoids default Windows path-length failures in deep virtual environments. Package
acceptance installs into a deliberately deep Windows path and loads the complete five-skill inventory from the installed wheel; source
and editable checkouts continue to use the readable `src/stagemesh/skill_packs` layout.

Publishing uses PyPI Trusted Publishing with GitHub OIDC. No long-lived PyPI token belongs in GitHub. The first `v0.6.0` upload created
the public PyPI project through this publisher identity, which remains the required release identity:

| Field | Value |
| --- | --- |
| PyPI project name | `stagemesh` |
| GitHub owner | `saketvishal` |
| GitHub repository | `stagemesh` |
| Workflow | `publish-pypi.yml` |
| Environment | `pypi` |

The project is now live at `https://pypi.org/project/stagemesh/`. StageMesh never asks for or stores the PyPI account password, recovery
codes, or an upload token.

Release order remains fail closed:

1. Merge the release candidate through protected `main` after its required Windows, Linux, and attribution checks pass.
2. From the verified merged commit, create and push the matching annotated tag (for example `v0.6.0`).
3. Publish a non-prerelease GitHub release for that exact tag. This explicit action triggers the PyPI workflow.
4. Require the `pypi` GitHub environment for the publish job. The workflow refuses mismatched tags, dirty provenance, multiple or
   unexpected distribution files, unsafe wheel members, installation failure, a missing console entry point, failed setup creation,
   or a false ready claim for an empty project.
5. After the workflow succeeds, independently install from the public index in a fresh environment:

   ```powershell
   python -m pip install --no-cache-dir stagemesh==0.6.3
   stagemesh --version
   stagemesh init
   ```

PyPI distributions are immutable. Never reuse a published version or enable `skip-existing`; a correction requires a new version.
