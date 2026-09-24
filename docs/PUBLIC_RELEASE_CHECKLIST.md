# Public release checklist

Use this checklist before advertising a StageMesh alpha release outside the repository.

## Product surface

- [ ] README value proposition matches current behavior.
- [ ] `WHY_STAGEMESH.md` reflects the actual architectural boundary.
- [ ] Setup commands are reproducible from a clean machine/environment.
- [ ] Examples use generic/public-safe paths and data.
- [ ] Public capability claims are backed by tests or execution evidence.

## Reliability gate

- [ ] Active P0/P1 lifecycle correctness issues required for the release are complete.
- [ ] Provider failure classification/failover behavior is accepted for supported runtimes.
- [ ] Concurrent integration and merge-conflict recovery behavior is accepted or clearly documented as a limitation.
- [ ] Restart/idempotency paths required by the release are tested.
- [ ] Exact-SHA and independent-review invariants pass.

## CI

- [ ] Pull-request CI is enabled.
- [ ] Main-branch CI is enabled.
- [ ] Supported Python/OS matrix is intentional.
- [ ] OSS boundary scan is green.
- [ ] CI status can be displayed truthfully in README.

## Versioning

- [ ] `pyproject.toml` version is authoritative.
- [ ] README/status text matches that version.
- [ ] Changelog contains the release.
- [ ] Git tag matches the package version.
- [ ] GitHub prerelease/release is created from the accepted commit.

## Distribution

- [ ] Installation path is verified.
- [ ] If publishing to PyPI, package name/version is available and upload succeeds.
- [ ] Clean-environment `pip install stagemesh` smoke test passes before README advertises it.
- [ ] `stagemesh --help`, `stagemesh init`, `stagemesh agent setup`, `stagemesh doctor`, and a minimal `stagemesh continue` path are smoke-tested.

## GitHub discovery

- [ ] Repository description is current.
- [ ] Topics are configured (for example: ai-agents, coding-agents, agent-orchestration, multi-agent, developer-tools, ai-coding, autonomous-agents, python).
- [ ] License is correctly detected by GitHub as Apache-2.0.
- [ ] Homepage/docs URL is set when a stable public docs destination exists.
- [ ] Structured issue forms are enabled.
- [ ] Discussions are enabled when there is enough outside usage to justify a community Q&A surface.

## Security/community

- [ ] SECURITY.md reporting path is usable.
- [ ] CODE_OF_CONDUCT.md is present.
- [ ] CONTRIBUTING.md matches the actual contribution workflow.
- [ ] No credentials, private repositories, private-product identifiers, or proprietary data are exposed.

## Final evidence

Record the release commit SHA, CI run, package artifact/version, smoke-test evidence, and any known limitations. A release is an evidence decision, not only a version bump.
