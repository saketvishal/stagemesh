# README information-architecture review

Reviewed on 2026-11-04 for issue #651. Length is evidence about maintenance and findability, not an acceptance target by itself.

## Before

The prior README contained 520 lines, 5,719 whitespace-delimited words, and 42,367 on-disk bytes. It mixed installation, first-run guidance, provider configuration, capability security, queue internals, ownership guarantees, repair procedures, and merge-policy reference material.

| Section | Lines | Words | Decision | Rationale and destination |
| --- | ---: | ---: | --- | --- |
| StageMesh vNext | 14 | 117 | Keep | Product identity, lifecycle, and exact-candidate guarantee belong at the entry point. |
| Quick Start | 45 | 247 | Shorten | Keep install and first run; link installer and versioning details. |
| Founder Operator Loop | 18 | 106 | Merge | Merge the short loop into Operating model; deep operation stays in `operator-flow.md`. |
| Founder Hands-Off | 11 | 115 | Shorten | Keep opt-in boundary and links; canonical detail stays in `founder-hands-off.md`. |
| Provider pools and task isolation | 22 | 178 | Shorten | Keep routing and independence summary; move reference detail to provider readiness. |
| Provider registry and selection policies | 79 | 904 | Move | Capability state, adapters, permissions, evidence, and pinning belong in `capabilities.md`; provider selection belongs in `provider-readiness.md`. |
| Choosing the next task | 22 | 202 | Shorten | Keep eligibility summary; exact selector behavior stays in `label-scope.md`. |
| Parallel execution | 50 | 547 | Move | Operational concurrency and delivery detail belongs in `operator-flow.md` and `run-window.md`. |
| stagemesh queue-run | 21 | 302 | Move | Raw queue execution is not the recommended first-run path; preserve its boundary in `operator-flow.md`. |
| Diagnosis before remediation | 37 | 459 | Shorten | Keep bounded diagnosis concept; move retry semantics to `recovery-budgets.md` and `reliable-delivery.md`. |
| Workspace ownership and external-mutation detection | 59 | 956 | Move | Preserve guarantees, known gaps, and explicit recovery in `workspace-and-task-recovery.md`. |
| Repairing a stuck task without touching SQLite | 67 | 979 | Move | Preserve doctor, rebind, baseline, ownership, and survivor-wave procedures in `workspace-and-task-recovery.md`. |
| Project profiles | 7 | 64 | Keep | Short, high-value project configuration entry point. |
| Packaging state for review | 12 | 114 | Shorten | Keep the redaction and schema boundary without field-by-field reference prose. |
| Project-controlled automatic merging | 56 | 429 | Move | Merge and landing policy already belongs in `run-window.md` and `reliable-delivery.md`. |

No section was classified Remove: every prior section contained either unique safety guidance or a useful entry point.

## After outline

The rewritten README contains 115 lines, 970 whitespace-delimited words, 8,363 on-disk bytes, and eight headings. The reduction comes from moving reference material behind explicit links, not from deleting an operational topic.

1. Product identity and exact-candidate lifecycle
2. Quick start
3. Operating model
4. Providers, skills, MCP, and plugins
5. Task selection, isolation, and parallel work
6. Recovery and safety boundaries
7. Profiles, handoff, and delivery
8. Documentation map

## Link migration map

| Prior topic | README entry point | Canonical detail |
| --- | --- | --- |
| Windows installer and stable package | Quick start | `project-local-runtime.md`, `versioning.md` |
| Founder daily and hands-off operation | Operating model | `operator-flow.md`, `founder-hands-off.md`, `continuous-driver.md` |
| Provider pools and stage commands | Providers, skills, MCP, and plugins | `provider-readiness.md` |
| Capability registry, skills, MCP, plugins | Providers, skills, MCP, and plugins | `capabilities.md`, `agent-skills.md`, `provider-output-security.md` |
| Task ranking and exact-label admission | Task selection, isolation, and parallel work | `label-scope.md` |
| Parallel, queue, and production windows | Task selection, isolation, and parallel work | `operator-flow.md`, `run-window.md`, `run-window-budgets.md` |
| Diagnosis and remediation budgets | Recovery and safety boundaries | `recovery-budgets.md`, `reliable-delivery.md` |
| Ownership, external mutation, task repair | Recovery and safety boundaries | `workspace-and-task-recovery.md` |
| Profiles | Profiles, handoff, and delivery | `profiles.md`, `profiles/caventra.md` |
| Merge and landing policy | Profiles, handoff, and delivery | `run-window.md`, `reliable-delivery.md` |

Every moved topic remains reachable directly from the README. The documentation map also links the current status and acceptance boundary so readers do not infer unverified product completeness from the quick start.
