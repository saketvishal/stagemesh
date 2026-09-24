# Contributing

Thank you for your interest in contributing to StageMesh.

## Before you start

This project is in **v0.2.0a1 (public alpha)** and the core architecture is still settling.
Before investing significant effort, open an issue to discuss your proposed change.

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

## Licensing of Contributions

StageMesh is licensed under the [Apache License, Version 2.0](LICENSE).

By submitting contributions (via pull requests, patches, documentation, or issue comments) to this project:
- You agree that your contributions are licensed under the Apache License, Version 2.0 terms, without any additional terms or conditions (in accordance with Section 5 of the Apache License 2.0).
- You represent that each contribution is your original creation, or that you have the right to submit it under the Apache License 2.0.
- No Contributor License Agreement (CLA) or copyright assignment is currently required; contributions operate under the standard open-source inbound=outbound licensing model.
- The project architecture remains compatible with adding formal Developer Certificate of Origin (DCO) sign-off (`Signed-off-by:`) or CLA automation in the future should project governance require it.
- For full details, see [docs/open-source/LICENSE_POLICY.md](docs/open-source/LICENSE_POLICY.md).

