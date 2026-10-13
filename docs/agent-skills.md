# Bundled agent skills

StageMesh command workers receive selected full skill files automatically. Adding a skill to an operator's chat is optional and does
not propagate it to a separate worker. After updating the installed StageMesh runtime, no per-task attachment is needed.

Installed projects can also own skills. A change contract may list complete, project-relative `SKILL.md` paths in `skill_refs`.
StageMesh validates that each path stays inside the task checkout and fails closed when an explicitly required skill is missing.
For frontend tasks, StageMesh automatically uses `agent-skills/genui-architect/SKILL.md` when the project provides it, with generated
runtime mirror locations as fallbacks. Project skills are read from the task's isolated checkout and never expand its allowed files,
acceptance criteria, or authority.

| Skill | Selection | Stage |
| --- | --- | --- |
| Anthropic `frontend-design` | UI file extensions, frontend JS/TS paths, or explicit UI intent; excludes documentation-only contracts | Implementation |
| Superpowers `systematic-debugging` | Bug/failure intent or a remediation attempt | Implementation |
| Superpowers `test-driven-development` | Code/behavior changes; excludes simple copy/style and documentation-only tasks | Implementation |
| Superpowers `verification-before-completion` | Every implementation and independent review | Both |

Selection uses the persisted title and the bound project's objective, allowed files, acceptance criteria, public API and spec references. Unknown
non-UI tasks still receive verification guidance. A generic "design component" objective or backend `app/` path alone does not select
frontend-design. The selection is deterministic; it is a relevance heuristic, not a model classifier.

## Authority and approved UI

The adapter places the user's instructions, applicable installed-project AGENTS/CLAUDE/GEMINI guidance, project-owned referenced
specs, approved UI and the bound contract ahead of upstream skill procedures. The agent must read the project guidance and referenced
specs before editing. StageMesh's own product specs are not the host application's specs.

Frontend guidance preserves approved tokens, layout, navigation and progressive disclosure. A menu or tool should appear only when
the user's role, available data or current task needs it. A skill does not grant permission to redesign an existing UI. Put allowed
surfaces and required visual/interaction checks in the project contract: skill prompts guide an agent but cannot guarantee adherence
or replace enforced file scope, validation gates and independent review. Never refresh an approved baseline just to hide a failure.

## Scoped engineering workflow

StageMesh retains scheduling, worker/worktree ownership, Git commits and merging. It does not load Superpowers' blanket
`using-superpowers` bootstrap or its brainstorming, parallel-agent, worktree or branch-finishing workflows into bounded workers.
Only the four engineering/design skills above are shipped. Native global plugins, hooks and automatic script execution are not installed.

The adapter explicitly overrides upstream full-suite-per-task instructions: run focused behavioral checks for changed surfaces and
affected callers, plus all required contract gates. Broaden for a failure, new change or unresolved risk. Do not repeat successful
checks with unchanged inputs. Simple copy/style edits do not require implementation-mirroring tests. Report commands, results and
validation limits. Reviews receive verification guidance only and remain read-only with their original structured response contract.

## Delivery and packaging

`src/stagemesh/agent_skills.py` selects the pack and appends absolute, JSON-quoted local SKILL.md paths to each command-provider prompt.
Codex/Claude receive these through their usual stdin prompt; Grok/Agy through their existing single-turn argument. Full bodies stay on
disk, keeping CLI arguments bounded. Providers read the files with their normal tools, or Python `Path.read_text` if a native Skill
tool is unavailable. Linked companions remain next to the selected skill. Example scripts are references, not runtime dependencies.

The wheel includes `stagemesh/skill_packs/`, including companion references, licenses and `manifest.json`. No runtime network fetch
or editable checkout is required. Before dispatch, StageMesh verifies each selected skill, companion and license against its exact
upstream Git blob recorded in the manifest. If a selected resource is missing/corrupted, implementation returns FAILED or review
returns INFRASTRUCTURE_FAILURE with `skill_pack_unavailable`; reinstall a complete runtime rather than continuing without guidance.
Damage to an unselected skill does not block an unrelated task.
Git attributes preserve the resource bytes across Windows checkouts so newline conversion cannot invalidate their provenance.

## Sources and updates

- [Anthropic frontend-design](https://github.com/anthropics/skills/tree/main/skills/frontend-design) is bundled unchanged with its Apache-2.0 license.
- [Superpowers](https://github.com/obra/superpowers/tree/main/skills) skills and references are bundled unchanged with Jesse Vincent's MIT license.
- Each resource's source URL and pinned Git blob SHA are in `src/stagemesh/skill_packs/manifest.json`; upstream `main` is not fetched during execution.

To update a snapshot, review the upstream changes, update resource bytes and manifest together, retain the licenses, build the wheel,
and run `tests/test_agent_skills.py` plus affected provider tests. The wheel consumer test loads all four skills in an isolated Python
process from extracted installed files, without the source on its import path. Transport tests cover the configured provider prompt
shapes. They verify selection and file delivery; they do not claim that a live model followed the instructions.
