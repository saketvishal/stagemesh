# Code Provenance and OSS-Boundary Audit

## Purpose

This document records the provenance and open-source-boundary audit conducted
for the StageMesh coordinator codebase (`build_coordinator/`) prior to public
release under the Apache License 2.0. It reflects checks that were actually
run against this repository, with their real output, not a narrative summary.

---

## 1. OSS-boundary scan (private-coupling canary)

`build_coordinator/oss_boundary.py` is a machine-checkable, AST-based scanner
that walks the `build_coordinator/` source tree looking for coupling to the
private product this codebase was originally extracted from: legacy
product-specific environment variable name prefixes, home-directory paths,
launcher names, and similar patterns. It runs in CI on every push (`Run OSS
boundary scan` step) and can be run locally:

```python
from build_coordinator.oss_boundary import evaluate_boundary
allowed, unexpected = evaluate_boundary()
```

**Result as of this audit:**

```
unexpected findings: 0
documented exceptions: 0
```

`DOCUMENTED_EXCEPTIONS` in `oss_boundary.py` is an empty frozenset, meaning no
tracked/undissolved coupling remains in the package.

### Legacy product-name occurrences (reviewed, not a violation)

`tests/test_independence.py::test_no_product_name_in_shipped_code_docs_or_project_definition`
enforces, as a hard CI gate, that the private product's name does not appear
anywhere in shipped files (case-insensitive, repo-wide). This document
intentionally does not spell out that name for the same reason — so it can
never accidentally reintroduce a match this test is designed to catch.

Two categories of file were reviewed for legitimate, non-leaking occurrences
of that name during this audit, both found benign and neither containing the
literal name in this document:

1. **`build_coordinator/oss_boundary.py` itself** — the scanner's own
   detection-rule source necessarily references the legacy name it is
   checking for (env var prefixes, path fragments, launcher names). This is
   the detection mechanism, not coupling.
2. **A handful of test fixtures** — these use the legacy product's name
   purely as an example organization/repo/project string to prove the
   generic task-source and backlog code handles arbitrary names correctly,
   including one that happens to collide with the name of the private
   product this codebase was extracted from. No private domain logic, case
   data, legal-intelligence content, or business logic appears in any of
   these fixtures — only a generic string literal used as test input.

Neither category embeds real private domain logic, strategy material,
case/user data, or credentials.

---

## 2. Secrets scan

Tracked source files (excluding docs and tests, which legitimately contain
allowlist/redaction field *names*) were searched for common secret patterns:
AWS access keys, PEM private key headers, GitHub tokens (`ghp_`), OpenAI-style
keys (`sk-`), and generic `password =` / `api_key =` assignments.

**Result:** no matches for actual secret material. The only hits were
field-name identifiers used for redaction/allowlisting logic
(`build_coordinator/execution/results.py`, `build_coordinator/runner/routing.py`
— e.g. the string `"api_key"` used as a key to *scrub* from persisted output),
not embedded secrets.

---

## 3. Third-party dependency license audit

Runtime dependencies declared in `pyproject.toml`, checked against installed
package metadata:

| Package | Scope | License |
|---|---|---|
| SQLAlchemy | required | MIT |
| Alembic | required | MIT |
| PyYAML | required | MIT |
| psycopg[binary] | optional (`postgres`, `dev` extras) | LGPLv3 |
| pytest | dev only | MIT |
| pytest-timeout | dev only | MIT |

All required runtime dependencies are MIT-licensed and impose no attribution
obligations beyond standard MIT notice preservation (satisfied by the
packages' own distributions; StageMesh does not vendor or redistribute their
source).

`psycopg[binary]` is LGPLv3 and is an **optional** dependency (PostgreSQL
support only; SQLite works without it). LGPLv3 permits use and dynamic
linking by an Apache-2.0-licensed application without imposing LGPL terms on
that application's own code, so this does not create a license conflict.
Because StageMesh does not vendor or modify psycopg source, no additional
NOTICE obligation is triggered — this is a normal LGPL-as-dependency
arrangement, the same one many Python projects have.

**Conclusion:** no dependency requires a NOTICE file entry or creates a
license-compatibility problem for Apache-2.0 distribution.

---

## 4. Architectural inspiration vs. implementation

During design, generic patterns common across multi-agent orchestration
systems (task claim/lease state machines, capability-based routing,
checkpoint/resume) informed StageMesh's architecture. No source code,
schemas, or substantial text was copied from any third-party project into
this repository. This repository was not diffed against every possible prior
system industry-wide; the claim here is limited to what was actually checked:
the OSS-boundary scan (organizational/product coupling) and a manual read of
the core modules below, not a formal clean-room legal certification.

Core modules reviewed for this audit and confirmed to be original,
first-party implementations specific to StageMesh's own JSON/git contracts:

- `build_coordinator/models.py`, `build_coordinator/db.py` — task/event
  persistence (SQLAlchemy models, this project's own schema)
- `build_coordinator/runner/routing.py` — capability-based worker routing
- `build_coordinator/execution/subprocess_executor.py`,
  `build_coordinator/execution/process_tree.py` — subprocess/process-tree
  lifecycle management (including the Windows Job Object based termination
  path)
- `build_coordinator/execution/results.py` — structured result-file contract
  and redaction rules
- `build_coordinator/project/backlog.py` — project-owned `.stagemesh/`
  backlog sync

---

## 5. Private/sensitive data check

No case data, user data, legal-intelligence content, patent-sensitive
material, or credentials were found in the tracked repository during this
audit (see sections 1–2). This is a repository-content check, not a
guarantee about untracked local files (`.build-coordinator/`, `.venv*`, etc.,
which are git-ignored and were not part of this scan).

---

## Conclusion

Based on the checks actually run and recorded above — the automated
OSS-boundary scanner (0 unexpected findings), a secrets pattern scan (no
matches), a dependency license review (all permissive, no NOTICE
obligation), and a manual read of the core modules — the tracked StageMesh
repository does not appear to contain private product logic, private data,
or unlicensed third-party code. This is an audit record, not a legal
opinion.
