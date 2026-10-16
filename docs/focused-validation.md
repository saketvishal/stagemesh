# Focused Validation For StageMesh Development

`AGENTS.md` controls investigation and discretionary testing. Required automated
validation comes from task contracts and the project profile, not agent promises.

The versioned `docs/profiles/stagemesh.profile.json` uses the existing profile
schema. Install it as this repository's `.stagemesh/profile.json` after backing
up the existing profile and confirming no task admission is in progress. This
is StageMesh's own development policy, not a default imposed on client projects.

New tasks can opt into a narrow, enforced scope and focused tests with one label:

| Label | Local validation |
| --- | --- |
| `validation:progress` | Progress rendering tests |
| `validation:attribution` | Attribution guards and attribution checker tests |
| `validation:core` | Lifecycle smoke gate |

Labels are deliberate, not guesses based on words in an issue. Unknown tasks,
multiple focused labels, and an explicit core label select the core fallback.
The narrow types cannot edit scheduler, persistence, schema or security files.
Work outside these categories needs an approved task-specific contract with its
actual affected tests, or a new reviewed profile type. Documentation-only tasks
should have relevant documentation/static gates, not unrelated application tests
or a whitespace-only placeholder passed off as executable validation.

Use `stagemesh profile --task ID` to inspect the selected type and gates before
admission. Existing prepared or frozen contracts are not rewritten by installing
a profile; they keep their required gates and evidence. Broader CI, independent
review and hosted landing checks remain unchanged. A focused gate does not prove
unrelated behavior, and failed checks must not be removed to make work pass.
