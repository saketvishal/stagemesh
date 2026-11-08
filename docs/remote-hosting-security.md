# Remote operator hosting security plan

Remote hosting remains disabled. `stagemesh serve` continues to bind only to loopback and has no host option. The new `stagemesh remote-hosting-plan --file PLAN.json` command only validates an operator-selected plan; it does not change a listener, invoke a provider, provision infrastructure, or store credentials.

## Threat model

The boundary assumes an untrusted network, malicious cross-origin pages, stolen browser state, compromised low-privilege accounts, accidental cross-project access, request flooding, oversized payloads, sensitive log capture, stale secrets, partial deployments, and failed restores. Arbitrary command execution, public anonymous access, shared multitenant hosting, and automatic migration from loopback are outside this design.

## Version 1 architecture

The validator supports one intentionally narrow architecture:

- a single-tenant reverse proxy owns TLS and forwards to StageMesh on a private loopback or equivalent private transport;
- OIDC Authorization Code with PKCE establishes identity;
- server-side sessions use Secure, HttpOnly, SameSite cookies and never browser local storage;
- read and control permissions are separate, project scoped, and default to read-only;
- secret values live only in an operator-selected secret store and rotate on a bounded schedule;
- reads, controls, and failures are audited without request or response bodies;
- request rate, payload size, audit retention, backup, restore-test, and atomic rollback limits are explicit.

The deployment hostname, OIDC issuer/client registration, secret store, backup target, and their operators are **PENDING OPERATOR DECISION**. A valid plan records those choices and produces a stable digest suitable for review. It is readiness evidence only; remote server implementation still requires a separately authorized change and security review.

## Negative acceptance requirements

- HTTP origins, embedded URL credentials, unknown fields, secret-shaped fields, missing controls, browser storage, combined read/control permission, disabled auditing, body logging, or unbounded limits fail validation.
- The loopback API remains the compatibility default.
- A remote implementation must independently test origin/CSRF enforcement, session fixation and expiry, project isolation, role escalation, audit redaction, rate/payload limits, secret rotation, backup restore, rollback, and direct-backend bypass.
- No URL, log, process title, repository file, or browser storage may contain a credential.
