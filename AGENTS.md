# Focused Development

## Read Only What The Change Needs

- Start with the task's acceptance criteria and working-tree status. Locate the relevant symbols, then read the affected code, direct callers or dependencies, and nearby tests.
- If the repository has a `.codegraph` index, use CodeGraph first to locate relevant code. Otherwise use targeted searches such as `rg` with file or directory filters.
- Do not dump, scan, or read the entire repository by default. Do not inspect generated files, dependency trees, test scratch directories, backups, or unrelated worktrees unless the task requires them.
- Expand investigation only when a specific dependency, failure, or uncertainty requires it. Reuse findings already established during the task instead of repeatedly reading unchanged files.

## Test In Proportion To Risk

- Run the smallest relevant regression tests first, then the affected test file or focused suite when warranted. Add tests for changed behavior, not unrelated coverage.
- Do not run the full suite, broad stabilization gates, real providers, or browser end-to-end suites for every change. Broaden checks only for affected shared behavior, cross-module contracts, explicit task requirements, or evidence that focused coverage is insufficient.
- For documentation-only changes, check accuracy, links, and any relevant documentation guards; do not run unrelated application tests.
- Avoid rerunning unchanged passing tests without a reason. A changed implementation, relevant new failure, or changed base can justify another run; record why.
- Let CI run its configured gates. Do not weaken or skip required validation, independent review, or safety checks to save tokens or time.
- Report the checks actually run and their results, plus any important untested risk. Do not claim broad verification from focused tests.
- When preparing new StageMesh tasks, use the relevant focused profile label or an approved task-specific contract; see `docs/focused-validation.md`. Do not label shared lifecycle or multi-area work as a narrow change just to reduce testing.

## Keep Work Bounded

- Keep edits scoped to the requested behavior. Avoid unrelated refactors, speculative abstractions, and repository-wide formatting or cleanup.
- Before expensive investigation or testing, state briefly what it will establish. Stop repeating checks once the required evidence is available.
- Whole-tree file hashing is for moves, recovery, or integrity verification when needed, not routine development.
- Follow the parent workspace lifecycle instructions: main is the only long-lived workspace, and temporary task directories must have a purpose and be removed safely when complete.
