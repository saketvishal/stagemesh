# Contributing

Thank you for your interest in contributing to StageMesh.

StageMesh is in public alpha and the architecture is still evolving. For substantial changes, open an issue first so scope, invariants, and acceptance criteria are clear.

## Development setup

```bash
git clone https://github.com/saketvishal/stagemesh.git
cd stagemesh
python -m pip install -e ".[dev,postgres]"
```

Run focused tests while developing:

```bash
pytest tests/test_relevant_area.py -v
```

Expand to dependent/contract tests when the change crosses module boundaries. Run the broader suite when justified by the repository validation policy or before release-level acceptance.

Do not weaken or delete tests merely to make a change pass.

## OSS boundary scanner

StageMesh includes a boundary scan intended to catch private-product coupling and forbidden content:

```bash
python -c "
from build_coordinator.oss_boundary import evaluate_boundary, format_failure
allowed, unexpected = evaluate_boundary()
if unexpected:
    raise SystemExit(format_failure(unexpected))
print(f'Boundary clean. {len(allowed)} documented exceptions.')
"
```

## Architectural constraints

StageMesh is built around firm invariants:

1. **Stages belong to StageMesh.** Agents and execution runtimes are replaceable infrastructure.
2. **Evidence advances the lifecycle.** Free-form agent confidence is not task authority.
3. **Routing is deterministic.** Worker selection follows configured capability, stage, permissions, provider health, and policy.
4. **Exact-SHA review matters.** Approval for one SHA cannot authorize another SHA.
5. **Independent review remains independent.** Builder/reviewer separation must not be bypassed.
6. **Provider/runtime failures are distinct from task failures.** Do not corrupt implementation/remediation accounting with infrastructure availability failures.
7. **Recovery is durable.** Preserve leases, checkpoints, events, git state, and restart/idempotency behavior.
8. **Secrets/private product data never enter public state.** Do not commit credentials, private source, Caventra-specific logic, or proprietary domain behavior.
9. **Git/integration safety fails closed.** Never bypass merge, SHA-drift, worktree, or review checks just to make automation progress.

See [AGENTS.md](AGENTS.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Test expectations

StageMesh uses several categories of tests:

- unit tests for deterministic functions and parsers;
- contract tests across lifecycle/module boundaries;
- integration tests for multi-step coordinator behavior;
- provider/runtime tests for execution and failover semantics;
- boundary/security tests for OSS/private separation and trust invariants.

During implementation, prefer the smallest test set that proves the changed behavior. Expand when risk or dependency impact requires it. Release-level acceptance should include the broad validation required by the release checklist.

## Submitting changes

1. Fork or branch the repository.
2. Open/associate an issue for non-trivial work.
3. Make a bounded change with appropriate tests.
4. Run focused and dependent validation.
5. Run the OSS boundary scanner when relevant.
6. Update public documentation if behavior/contracts changed.
7. Open a pull request using the repository template.

Pull requests should explain the lifecycle/architecture impact and include deterministic validation evidence.

## Code style

- Python 3.11+
- Use `from __future__ import annotations` in modules where the codebase convention requires it.
- Prefer explicit typed data/contracts over implicit dict conventions where practical.
- Keep deterministic policy/routing logic separate from agent/model reasoning.
- Keep functions focused and add docstrings to public API functions/classes.

## Conduct and security

Participation is subject to [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

Report vulnerabilities through the private path described in [SECURITY.md](SECURITY.md). Do not open a public issue containing credentials, exploit details, private source, or sensitive project data.

## License

By contributing, you agree that your contributions will be licensed under the Apache-2.0 license.
