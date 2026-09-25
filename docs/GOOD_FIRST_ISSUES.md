# Good first issue backlog

This is the curated public-alpha on-ramp for new StageMesh contributors. Each
entry is intended to be small, isolated, and understandable without Caventra,
private repositories, provider credentials, or paid model access.

When opening these as GitHub issues, apply both labels:

- `good first issue`
- `help wanted`

Do not apply those labels to lifecycle, branch-integrity, security, migration,
or release-publishing work unless it has been separately reduced to a safe,
standalone task with the same level of setup, acceptance criteria, and
validation shown here.

## Audit rationale

The local worktree does not include live GitHub issue metadata, and the GitHub
CLI is not available to this isolated task runner. Rather than mislabel unknown
open issues, this backlog defines eight narrow contributor tasks that can be
opened or copied into GitHub. The selected areas are documentation, examples,
CLI help, and focused unit tests around already-public behavior. They avoid
coordinator-critical state transitions, branch safety, security boundaries,
migrations, and publishing.

## Curated entries

### 1. Document a no-provider local smoke path

Labels: `good first issue`, `help wanted`

Context and reproduction:

1. Create a fresh virtual environment.
2. Run `pip install -e ".[dev]"`.
3. Run `stagemesh --version`.
4. Run a tiny no-provider check such as `pytest tests/test_launchers.py -q`.

Likely area:

- `README.md`
- `docs/SETUP.md`
- `CONTRIBUTING.md`

Acceptance criteria:

- The docs include a short path that confirms the package and CLI work without
  provider credentials or paid model access.
- The path names the smallest validation command.
- The wording does not imply PyPI publication is complete.
- No Caventra or private-repository context is introduced.

Validation:

```bash
pytest tests/test_launchers.py -q
```

Windows/Linux notes:

- Use `.venv\Scripts\activate` on Windows and `source .venv/bin/activate` on
  Linux/macOS if activation is shown.

### 2. Add an examples index smoke test

Labels: `good first issue`, `help wanted`

Context and reproduction:

1. Open `examples/README.md`.
2. Check that every linked example directory exists.
3. Check that each example README mentioned by the index still exists.

Likely area:

- `examples/README.md`
- `tests/test_examples_index.py` or another focused docs-link test

Acceptance criteria:

- A small test fails when the examples index points at a missing local example.
- The test uses local filesystem checks only.
- Existing example wording remains intact except for necessary link fixes.

Validation:

```bash
pytest tests/test_examples_index.py -q
```

Windows/Linux notes:

- Use `pathlib.Path` so path separators are portable.

### 3. Add a CONTRIBUTING command freshness test

Labels: `good first issue`, `help wanted`

Context and reproduction:

1. Open `CONTRIBUTING.md`.
2. Find fenced command snippets that reference tests or the boundary scanner.
3. Compare them with the commands contributors are expected to run today.

Likely area:

- `CONTRIBUTING.md`
- `tests/test_contributing_docs.py`

Acceptance criteria:

- A focused test verifies the newcomer validation commands documented in
  `CONTRIBUTING.md` are present.
- The test does not execute long-running full-suite commands.
- The docs continue to mention the boundary scanner and at least one targeted
  pytest command.

Validation:

```bash
pytest tests/test_contributing_docs.py -q
```

Windows/Linux notes:

- Avoid shell-specific assertions. Match command text in the markdown instead.

### 4. Clarify worker example prerequisites

Labels: `good first issue`, `help wanted`

Context and reproduction:

1. Read `examples/single_agent/README.md`.
2. Read `examples/staged_multi_agent/README.md`.
3. Identify the first command a reader can run without model credentials.

Likely area:

- `examples/single_agent/README.md`
- `examples/staged_multi_agent/README.md`
- `examples/README.md`

Acceptance criteria:

- Each example README clearly separates local configuration inspection from
  provider-backed execution.
- The first validation step works without provider credentials.
- Provider-specific steps explicitly say credentials are required.

Validation:

```bash
pytest tests/test_oss_boundary.py -q
```

Windows/Linux notes:

- Prefer commands that work in both PowerShell and POSIX shells, or show both
  forms when environment variables are involved.

### 5. Add CLI help coverage for public aliases

Labels: `good first issue`, `help wanted`

Context and reproduction:

1. Run `stagemesh --help`.
2. Run `build-coordinator --help`.
3. Confirm both commands describe the same public CLI surface.

Likely area:

- `tests/test_launchers.py`
- `tests/test_global_and_onboarding.py`
- `build_coordinator/cli.py`

Acceptance criteria:

- A focused test covers `--help` or equivalent help output for both console
  script names.
- The test does not require a configured project or providers.
- Any wording change keeps `stagemesh` as the primary public CLI and
  `build-coordinator` as a compatibility alias.

Validation:

```bash
pytest tests/test_launchers.py -q
```

Windows/Linux notes:

- Console script wrappers differ by platform, so prefer invoking the CLI module
  or existing launcher helpers used by the test suite.

### 6. Improve setup docs for editable installs

Labels: `good first issue`, `help wanted`

Context and reproduction:

1. Create a clean checkout.
2. Run `pip install -e ".[dev]"`.
3. Run one targeted test command.

Likely area:

- `docs/SETUP.md`
- `README.md`
- `CONTRIBUTING.md`

Acceptance criteria:

- The setup docs name Python 3.11+ before install commands.
- The editable install command is consistent across docs.
- The first validation command is targeted and quick.
- The docs distinguish source installs from future PyPI installs.

Validation:

```bash
pytest tests/test_packaging_metadata.py -q
```

Windows/Linux notes:

- Quote `".[dev]"` in examples so shells do not expand brackets.

### 7. Add README documentation-link checks

Labels: `good first issue`, `help wanted`

Context and reproduction:

1. Open `README.md`.
2. Review the Documentation section.
3. Check that every relative markdown link points to an existing file.

Likely area:

- `README.md`
- `tests/test_readme_links.py`

Acceptance criteria:

- A small test verifies local relative documentation links in the README.
- External links, if any are added later, are ignored by the test.
- Broken links produce a clear assertion message naming the missing target.

Validation:

```bash
pytest tests/test_readme_links.py -q
```

Windows/Linux notes:

- Use `pathlib` and avoid case assumptions that only pass on one filesystem.

### 8. Make boundary scanner failures easier to read

Labels: `good first issue`, `help wanted`

Context and reproduction:

1. Run the boundary scanner command from `CONTRIBUTING.md`.
2. Read `build_coordinator/oss_boundary.py`.
3. Inspect `tests/test_oss_boundary.py` for expected failure formatting.

Likely area:

- `build_coordinator/oss_boundary.py`
- `tests/test_oss_boundary.py`
- `CONTRIBUTING.md`

Acceptance criteria:

- Boundary scanner failure output groups findings by file or otherwise makes
  multiple findings easier to scan.
- Existing documented exceptions remain allowed.
- The change does not weaken detection rules.
- Tests cover the updated formatting.

Validation:

```bash
pytest tests/test_oss_boundary.py -q
```

Windows/Linux notes:

- Normalize paths in tests so failure messages are stable across platforms.

## Labeling checklist

Before applying `good first issue` and `help wanted`, confirm the issue has:

- A 10-15 minute reproduction or setup path where feasible.
- Clear files or area likely involved.
- Explicit acceptance criteria.
- The smallest relevant validation command.
- Windows/Linux notes when commands or paths differ.
- No need for Caventra/private context, secrets, provider credentials, or paid
  model access unless explicitly marked as optional.
