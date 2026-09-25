# StageMesh Open-Source License Policy

## Overview

StageMesh is licensed under the [Apache License, Version 2.0](../../LICENSE).

This document outlines the licensing scope, dependency standards, contribution terms, and separation boundaries for the standalone coordinator.

---

## What Is Covered

The Apache License 2.0 covers all source code, tests, documentation, and configurations included within this standalone repository:

- The StageMesh coordinator core (`build_coordinator/`)
- Multi-provider agent routing and execution drivers
- Task state machine, SQLite/PostgreSQL persistence, and lease management
- Checkpoint/recovery and independent review mechanics
- CLI tooling and project backlog definitions (`stagemesh` command and `.stagemesh/` schemas)
- Non-proprietary tests, architecture specifications, and documentation

---

## What Is Not Covered

The open-source license explicitly **does not cover**:

- Trademarks, trade names, or service marks associated with StageMesh or its maintainers (per Section 6 of Apache-2.0).
- Proprietary application logic or downstream systems that consume or integrate with StageMesh.
- Any confidential intellectual property, trade secrets, domain intelligence, or data held in private repositories.

---

## Relationship to Private Product Repositories

StageMesh was originally conceived to solve durable multi-agent orchestration for a private product monorepo before being extracted into this standalone, provider-neutral project.

1. **Strict Decoupling**: StageMesh is domain-neutral infrastructure. It contains zero product-specific application logic, proprietary domain intelligence, confidential workflows, case or client data, patent drafts, or private reasoning datasets.
2. **Automated Boundary Verification**: The repository includes continuous automated verification (`build_coordinator.oss_boundary` and independence tests) ensuring no private environment variables, paths, or product couplings enter this codebase.
3. **Consuming Architecture**: Downstream private systems consume StageMesh as an independent orchestrator via public extension points (CLI invocations, `.stagemesh/` project backlogs, and runner configs), preserving complete isolation between private IP and open-source infrastructure.

---

## Third-Party Dependencies

StageMesh adheres to strict open-source dependency hygiene:

- **Permissive Core**: Direct runtime dependencies are restricted to established, permissively licensed libraries (MIT, Apache-2.0, BSD).
- **No Bundled Third-Party Code**: No external libraries or third-party source distributions are vendored into the repository tree.
- **Optional Extensions**: Optional adapters (such as `psycopg` for PostgreSQL, licensed under LGPLv3) are maintained as optional extras (`[postgres]`) dynamically imported at runtime and are not required for core coordinator operation.
- **License Scans**: Continuous automated license and provenance tracking ensures incoming dependencies do not impose reciprocal copyleft (GPL/AGPL/SSPL) on users of the Apache-2.0 orchestrator.

---

## Contribution Licensing Policy

StageMesh follows the inbound=outbound licensing model:

- All contributions submitted to this repository (via pull requests, patches, or issues) are licensed under the Apache License, Version 2.0, under the terms of Section 5 of the Apache License.
- No Contributor License Agreement (CLA) or copyright assignment is currently required.
- Maintainers reserve the right to introduce formal Developer Certificate of Origin (DCO) sign-offs or CLA verification if required by future governance structures.

---

## Source File Headers

To reduce code noise and maintenance overhead, individual source files in this repository do not require redundant multi-line license boilerplate. The root [LICENSE](../../LICENSE) file and package metadata in `pyproject.toml` govern all files in the repository.

Where file-level attribution is desired, the standard machine-readable SPDX identifier should be used:

```python
# SPDX-License-Identifier: Apache-2.0
```
