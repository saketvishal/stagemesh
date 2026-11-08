# CI optimization foundations

StageMesh can compute a read-only, bounded batch-size recommendation from explicit queue-delay, runner-utilization, and recent-failure measurements. A high combined-candidate failure rate always takes precedence over runner pressure. The recommendation never changes an active window.

The `stagemesh ci-optimize-plan` command reports the recommendation and `mutation_performed=false`. An operator or a later, separately reviewed controller may choose whether to apply it.

The optimization library also provides:

- a deterministic failing-batch bisector with a hard probe budget;
- a versioned, content-addressed cache-manifest parser;
- candidate, policy, platform, target, path, size, and SHA-256 checks before cached evidence can be reused.

These are safety foundations, not claims of a deployed distributed cache. This change does not configure a cache service, collect live GitHub queue latency, publish diagnostic subsets, or start speculative re-validation. Those integrations need separate backend, cost, authorization, and retention decisions.

Example read-only planning call:

```text
stagemesh ci-optimize-plan --current-size 4 --max-size 8 --queue-delay-seconds 900 --runner-utilization 0.9 --recent-failure-rate 0 --json
```
