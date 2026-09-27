# PyPI Release Evidence

## Current package decision

- Distribution name: `stagemesh`
- Version: `0.2.0a1`
- Import package: `build_coordinator`
- Primary console script: `stagemesh`
- Compatibility console script retained for this alpha: `build-coordinator`

These values are declared in `pyproject.toml` and guarded by
`tests/test_packaging_metadata.py`.

## Publication path

`.github/workflows/publish-pypi.yml` builds the source distribution and wheel,
verifies the built wheel metadata and both console entry points, uploads those
exact artifacts, and publishes them to PyPI with PyPI Trusted Publishing through
GitHub Actions OIDC. No long-lived PyPI token is required or stored in the
repository.

## Artifact provenance baseline

Release artifacts are produced only from the repository contents checked out by
GitHub Actions for `.github/workflows/publish-pypi.yml`. The build job uses
Python 3.12 on `ubuntu-latest`, installs the `build` frontend, and runs
`python -m build` against `pyproject.toml`. The declared build backend is
`hatchling.build`; wheel package contents come from the `build_coordinator`
package selected under `[tool.hatch.build.targets.wheel]`.

The workflow expects exactly one `stagemesh-*.tar.gz` source distribution and
one `stagemesh-*.whl` wheel in `dist/`. Before upload, it verifies that the
wheel metadata names the `stagemesh` distribution, that both console scripts
resolve to `build_coordinator.cli:main`, and that the source distribution
contains `pyproject.toml`. The publish job downloads the
`python-package-distributions` artifact produced by the build job and publishes
those files to PyPI.

CI provenance for the source tree is limited to `.github/workflows/ci.yml`,
which runs tests on Ubuntu and Windows for Python 3.11 and 3.12, performs the
OSS boundary scan, and imports representative runtime modules. The current
release process does not claim an SBOM, SLSA provenance attestation, signed
artifact bundle, or reproducible-build guarantee; add those only after the
corresponding workflow evidence exists.

## Local verification log

2026-09-25 worktree verification:

```text
python -c "from tests.test_packaging_metadata import test_public_distribution_metadata_keeps_compatible_cli_alias; test_public_distribution_metadata_keeps_compatible_cli_alias(); print('packaging metadata assertions: OK')"
# packaging metadata assertions: OK
# exit 0

python -m build
# failed: No module named build

uv run --with build python -m build
# failed: network unavailable while resolving dependencies from PyPI

python -m pytest tests/test_packaging_metadata.py -q
# failed: No module named pytest

uv run --with pytest python -m pytest tests/test_packaging_metadata.py -q
# failed: network unavailable while resolving dependencies from PyPI
```

The metadata regression test uses only the Python standard library and passed
directly. Building artifacts and running pytest require build/test dependencies
that were not installed in this isolated worktree, and network access to PyPI
was unavailable, so this worktree cannot honestly claim a successful local
artifact build yet.

Record the final verification here when preparing a release candidate:

```bash
python -m build
python -m pip install --force-reinstall --no-deps dist/stagemesh-0.2.0a1-py3-none-any.whl
stagemesh --version
stagemesh doctor --json
build-coordinator --version
```

After PyPI publication, verify the documented install path from a brand-new
environment:

```bash
python -m venv /tmp/stagemesh-pypi-smoke
/tmp/stagemesh-pypi-smoke/bin/python -m pip install --upgrade pip
/tmp/stagemesh-pypi-smoke/bin/python -m pip install stagemesh
/tmp/stagemesh-pypi-smoke/bin/stagemesh --version
/tmp/stagemesh-pypi-smoke/bin/stagemesh doctor --json
```

On Windows, use `Scripts\python.exe` and `Scripts\stagemesh.exe` inside the
virtual environment.

`stagemesh doctor` may exit non-zero when no agents or projects are configured;
for the install smoke, the relevant evidence is that the installed `stagemesh`
console script resolves to `build_coordinator.cli:main`, imports successfully,
and emits the expected diagnostic report rather than failing to start.

## HUMAN_ACTION_REQUIRED

Before declaring PyPI availability in public launch traffic, a repository owner
must complete these PyPI-only steps:

1. Create or claim the `stagemesh` project on PyPI.
2. Configure a PyPI Trusted Publisher for this GitHub repository, workflow
   `.github/workflows/publish-pypi.yml`, environment `pypi`.
3. Publish the GitHub release or manually dispatch the workflow for version
   `0.2.0a1`.
4. Confirm the workflow published `stagemesh-0.2.0a1.tar.gz` and
   `stagemesh-0.2.0a1-py3-none-any.whl` on PyPI.
5. Run the brand-new environment install smoke above with
   `python -m pip install stagemesh` and paste the command output into this
   document.
