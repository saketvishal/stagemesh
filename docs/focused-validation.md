# Focused Development And Validation

## Default In Every Client Repository

StageMesh delivers focused-reading and proportional-testing instructions to both
implementation and review providers, without requiring a client `AGENTS.md`,
profile, labels, or configuration migration. Client instructions and required
contract gates remain authoritative. The policy is not a sandbox or a claim that
an agent cannot read unrelated code.

For concrete affected paths, StageMesh probes exact conventional nearby test
filenames and package metadata, never a recursive repository search. Python
projects declaring pytest and JavaScript packages declaring plain Vitest/Jest
test scripts receive relevant local command suggestions. Validation also checks
the exact candidate's Git tree for these tests and adds uncovered checks to the
normal candidate validation. A failed discovered check fails validation.

Nested packages keep their own working directory; matching declared pytest
commands supply their interpreter and environment. Existing contract gates are
never replaced by discovery. Exact targets and recognized suite coverage avoid
redundant checks; ambiguous filters, wrappers, missing tests, unsupported runners
and discovery limits fall back to the existing gates. Convention matching cannot
prove complete affected-caller coverage: agents must inspect direct callers and
reviewers must check the selected tests. Documentation-only paths do not trigger
application-test discovery. Integration tests are not guessed from source names.

This runtime behavior is generic: there are no client names, paths or suites in
the selection code. Clients receive it when their installed StageMesh package is
updated, not merely because this repository's profile changed. Running processes
must finish before a shared installation is upgraded.

## Invariants Are Not All Commands

Prefix natural-language contract invariants with `review:`. These are mandatory
review constraints, not shell programs. Validation records them as
`review_invariants`, and implementation and
review prompts retain their original text. They require an actual reviewer; the
builtin deterministic fallback cannot approve them on its own. Frozen contract
text and hashes are not rewritten by this interpretation.

Executable checks belong in `required_tests`, `lint`, `typecheck` or
`dependency_checks`. Clear legacy command-shaped invariant strings (runners such as
`python`, executable paths and script filenames, bare commands, or a command
followed immediately by a flag) still execute and must
pass. Use `command: validator check` for an otherwise ambiguous custom command,
or `review: python compatibility must remain unchanged` for prose that begins
with a command name. Unprefixed ambiguous strings, including complete English
sentences and positional commands such as `validator check`, are rejected with
an actionable contract error, not executed or silently converted to prose. An
existing frozen contract needs an approved audited rebind to classify such text;
the runtime never rewrites historical bindings or evidence automatically. Missing
executables and malformed explicit commands fail closed; no required command is
converted into a passing result.

## Optional StageMesh Self-Development Profile

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
