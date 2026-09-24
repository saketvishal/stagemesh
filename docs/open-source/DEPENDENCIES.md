# Dependency License Audit

This document inventories all runtime, optional, and development dependencies for StageMesh (derived from `pyproject.toml` and `uv.lock`), assessing license compatibility with Apache License 2.0 distribution and tracking attribution/NOTICE obligations.

---

## 1. Direct Runtime Dependencies

These are required for basic coordinator execution (SQLite persistence, configuration, and migrations):

| Dependency | Configured Range | Locked Version | License | Source / Upstream | Apache-2.0 Compatible? | Attribution / NOTICE Obligations |
|---|---|---|---|---|---|---|
| **SQLAlchemy** | `>=2.0` | `2.0.54` | MIT | [sqlalchemy.org](https://www.sqlalchemy.org) | Yes | None for source distribution. MIT copyright notice retained in upstream package. |
| **Alembic** | `>=1.13` | `1.20.0` | MIT | [alembic.sqlalchemy.org](https://alembic.sqlalchemy.org) | Yes | None for source distribution. MIT copyright notice retained in upstream package. |
| **PyYAML** | `>=6.0` | `6.0.3` | MIT | [pyyaml.org](https://pyyaml.org/) | Yes | None for source distribution. MIT copyright notice retained in upstream package. |

---

## 2. Transitive Runtime Dependencies

These are automatically installed as dependencies of SQLAlchemy and Alembic:

| Dependency | Locked Version | Required By | License | Source / Upstream | Apache-2.0 Compatible? | Attribution / NOTICE Obligations |
|---|---|---|---|---|---|---|
| **greenlet** | `3.5.6` | SQLAlchemy | MIT & Python-2.0 | [greenlet.readthedocs.io](https://greenlet.readthedocs.io/) | Yes | Permissive. None for source distribution. |
| **typing-extensions** | `4.16.0` | SQLAlchemy, Alembic | PSF-2.0 | [github.com/python/typing_extensions](https://github.com/python/typing_extensions) | Yes | Permissive Python Software Foundation License. |
| **Mako** | `1.4.3` | Alembic | MIT | [makotemplates.org](https://www.makotemplates.org/) | Yes | Permissive MIT. |
| **MarkupSafe** | `3.0.3` | Mako | BSD-3-Clause | [palletsprojects.com](https://palletsprojects.com/) | Yes | Permissive BSD 3-Clause. |

---

## 3. Optional Dependencies (`[postgres]`)

StageMesh defaults to SQLite, requiring zero external database drivers. Users targeting PostgreSQL opt-in to `psycopg`:

| Dependency | Configured Range | Locked Version | License | Source / Upstream | Apache-2.0 Compatible? | Status / Review Notes |
|---|---|---|---|---|---|---|
| **psycopg** | `>=3.1` | `3.3.6` | LGPL-3.0-or-later | [psycopg.org](https://psycopg.org/psycopg3/) | Yes (dynamic library use) | **FLAGGED FOR AWARENESS (LGPLv3)**: Psycopg is dynamically imported as an optional adapter. Core coordinator does not statically link or vendor psycopg code. Compatible with Apache-2.0 under LGPL Section 4. |
| **psycopg-binary** | `>=3.1` | `3.3.6` | LGPL-3.0-or-later | [psycopg.org](https://psycopg.org/psycopg3/) | Yes (dynamic library use) | **FLAGGED FOR AWARENESS (LGPLv3)**: Pre-compiled wheel binary distribution of psycopg driver. |
| **tzdata** | *(transitive)* | `2026.4` | Apache-2.0 | [github.com/python/tzdata](https://github.com/python/tzdata) | Yes | Apache-2.0. |

---

## 4. Development & Test Dependencies (`[dev]`)

Used exclusively for test execution, boundary scanning, and local development; never bundled into runtime distributions:

| Dependency | Configured Range | Locked Version | License | Source / Upstream | Apache-2.0 Compatible? | Attribution / NOTICE Obligations |
|---|---|---|---|---|---|---|
| **pytest** | `>=8.0` | `9.1.1` | MIT | [docs.pytest.org](https://docs.pytest.org/) | Yes | Dev/Test only. |
| **pytest-timeout** | `>=2.3` | `2.4.0` | MIT | [github.com/pytest-dev/pytest-timeout](https://github.com/pytest-dev/pytest-timeout) | Yes | Dev/Test only. |
| **colorama** | *(transitive)* | `0.4.6` | BSD-3-Clause | [github.com/tartley/colorama](https://github.com/tartley/colorama) | Yes | Dev/Test only. |
| **iniconfig** | *(transitive)* | `2.3.0` | MIT | [github.com/pytest-dev/iniconfig](https://github.com/pytest-dev/iniconfig) | Yes | Dev/Test only. |
| **packaging** | *(transitive)* | `26.3` | Apache-2.0 or BSD-2-Clause | [packaging.pypa.io](https://packaging.pypa.io/) | Yes | Dev/Test only. |
| **pluggy** | *(transitive)* | `1.6.0` | MIT | [github.com/pytest-dev/pluggy](https://github.com/pytest-dev/pluggy) | Yes | Dev/Test only. |
| **pygments** | *(transitive)* | `2.21.0` | BSD-2-Clause | [pygments.org](https://pygments.org/) | Yes | Dev/Test only. |

---

## 5. Copyleft / Reciprocal License Summary

- **GPL / AGPL / SSPL**: Zero dependencies.
- **LGPL**: `psycopg` and `psycopg-binary` (v3.3.6) are licensed under LGPLv3.
  - *Analysis*: Used exclusively as an optional database driver when an operator chooses PostgreSQL instead of the default SQLite. It is imported dynamically through standard Python import semantics; StageMesh does not vendor, modify, or create a derived work of psycopg. Under LGPLv3 Section 4 ("Combined Works"), dynamic linking from an Apache-2.0 application is compliant and does not require re-licensing the coordinator.
- **Proprietary / Unknown**: Zero dependencies.

---

## 6. NOTICE / Attribution Determination

Neither the core runtime dependencies nor development dependencies require an embedded `NOTICE` file or `THIRD_PARTY_NOTICES` in the StageMesh source tree:
1. No third-party source code is vendored or bundled into the repository.
2. Direct dependencies carry standard MIT / BSD / Apache / PSF licenses whose notice obligations are satisfied by their respective upstream distributions on PyPI.
3. Therefore, no `NOTICE` or `THIRD_PARTY_NOTICES` file is required in this repository.
