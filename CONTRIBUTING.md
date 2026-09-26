# Contributing

Thank you for your interest in contributing to StageMesh.

## Before you start

This project is in **v0.1-alpha** and the core architecture is still settling.
Before investing significant effort, open an issue to discuss your proposed change.

## New contributor path

Start with the curated [good first issue backlog](docs/GOOD_FIRST_ISSUES.md).
Those entries are intentionally small, public-alpha friendly, and labeled for
new contributors only when they do not require private context, provider
credentials, paid model access, or deep coordinator internals.

Repository labels and superseded issue/PR handling are maintained through the
[GitHub housekeeping playbook](docs/GITHUB_HOUSEKEEPING.md). In short: link
duplicates to their canonical issue, preserve discussion, and do not mark work
validated merely because an implementation exists locally.

For your first contribution:

1. Choose an issue labeled both `good first issue` and `help wanted`.
2. Follow the issue's reproduction/setup path before changing code or docs.
3. Keep the change scoped to the listed files or area unless the investigation
   shows a nearby test or doc also needs a small update.
4. Run the smallest validation command named in the issue, plus any directly
   related tests you changed.
5. Open a pull request that includes what changed, how you validated it, and
   any Windows/Linux difference you noticed.

StageMesh uses independent review as part of its engineering workflow. Expect a
reviewer who did not author your change to validate the result against the issue
acceptance criteria before integration.

## Development setup

```bash
git clone <repo-url>
cd <repo-directory>

# Install with development dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v
```

## Running the boundary scanner

```bash
python -c "
from build_coordinator.oss_boundary import evaluate_boundary, format_failure
allowed, unexpected = evaluate_boundary()
if unexpected:
    raise SystemExit(format_failure(unexpected))
print(f'Boundary clean. {len(allowed)} documented exceptions.')
"
```

## Test requirements

All contributions must include appropriate tests. The test suite must remain
fully green before any change is considered.

StageMesh has several categories of tests:
- **Unit tests**: individual service functions, routing, config parsing
- **Integration tests**: multi-step lifecycle flows against a real SQLite database
- **Boundary tests**: automated scans for private-IP coupling and forbidden imports

Do not delete or weaken tests to make a change pass.

## Architectural constraints

StageMesh is designed around a small number of firm principles:

1. **Stages belong to the coordinator** - the coordinator owns the task lifecycle.
   Agents are executors that receive prompts and write structured results.

2. **Routing is deterministic** - worker selection is based on configured
   capabilities, never on a model inference result.

3. **Secrets never enter persistent state** - config may reference env vars or
   credential helpers; secrets must not appear in databases, checkpoints, events,
   logs, results, or config files.

4. **Human gates are blocking** - automation can prepare, but cannot approve.
   Human escalation types are explicit and durable.

5. **Structured results only** - the coordinator never trusts free-form stdout.
   Workers write a JSON result file to a runner-generated path.

Changes that violate these principles will not be accepted regardless of other merit.

## Submitting changes

1. Fork the repository
2. Create a feature branch
3. Make your changes with tests
4. Ensure `pytest tests/ -v` passes completely
5. Ensure the boundary scanner reports clean
6. Open a pull request with a clear description of what changed and why

## Code style

- Python 3.11+
- Use `from __future__ import annotations` in all modules
- Prefer `dataclasses.dataclass(frozen=True)` for value objects
- Keep functions focused; avoid large functions
- Add docstrings to public API functions and classes

## License

By contributing, you agree that your contributions will be licensed under the
Apache-2.0 license.
