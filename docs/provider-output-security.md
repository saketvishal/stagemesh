# Provider output security boundary

Provider output is untrusted text. StageMesh may retain a bounded excerpt for diagnosis, but it must not treat that text as a command, an authorization, or a source of recovery actions.

Before persistence, `sanitize_provider_output` converts the value without propagating conversion errors, normalizes line endings, replaces unsafe control characters, scrubs credentialed URLs, credential assignments, bearer values, and common provider-token prefixes, and then applies a deterministic head/tail bound. Classification and review verdict parsing use the original in-memory response; only the recorded diagnostic copy is sanitized.

The boundary applies to provider failure and no-progress events, implementation recovery receipts, pool-exhaustion evidence, reviewer responses and infrastructure-failure envelopes. Audit export and cooldown reporting sanitize again so legacy rows written before this boundary cannot re-expose their raw text. The activity and operator APIs continue to expose only typed allowlisted fields rather than arbitrary provider output.

Provider-output changes are shared security-boundary changes. The audited CI planner therefore selects the full Windows and Linux regression, and both protected jobs run `tests/test_provider_output_security.py` as a named group. Those tests use adversarial custom-provider text and verify persistence, review classification, legacy read projections, control-character handling, bounding, and redaction independently of built-in provider behavior.

This is credential-leak reduction, not a general data-loss-prevention claim. Unknown token formats and secrets that do not match the declared patterns remain outside the guarantee; callers should avoid placing secrets in provider output and should use least-privilege provider credentials.
