# Atomic scope and diff-first quarantine

StageMesh treats coding providers as untrusted contributors. The provider works in a task-owned Git worktree; StageMesh creates the
candidate commit, independently reads the exact baseline-to-candidate Git diff, validates the frozen contract, and requires exact-SHA
validation and review before integration. Provider explanations are not evidence.

## Current coverage

| Requirement | Status | Enforcement |
|---|---|---|
| Separate task worktrees, durable ownership and restart recovery | Present | `workspaces.py`, `workspace_guard.py`, recovery and ownership tests |
| Pinned baseline and candidate SHA | Present | frozen contract bindings and exact-SHA evidence |
| Allowed, forbidden, protected and excluded paths | Present | deterministic contract evaluation before review/integration |
| STRICT / BOUNDED / EXPLORATORY write modes | Present | opt-in Atomic Scope fields in the task contract |
| Read scope distinct from write scope | Present in the provider contract | common provider prompt; reads cannot be reconstructed perfectly after an arbitrary local CLI exits |
| Function/symbol write scope | Present for Python | AST-backed comparison; unsupported languages or unparseable revisions fail closed for independent approval |
| Generated files and approved expansion | Present | separately classified paths, owner, revision, justification and policy hash |
| Rename, binary, symlink, submodule and gate-config inspection | Present | normalized exact-SHA diff inspection |
| Staged, unstaged, untracked and ignored worktree state | Present | independent task-worktree snapshot; ignored files are reported as not part of the candidate |
| Out-of-scope quarantine | Present | validation failure plus durable `refs/stagemesh/quarantine/...` reference and audit event |
| Change-inflation expectations | Present | task contract file/line expectations and diff risk flags |
| Per-task branch name | Intentionally not used | isolated worktrees are detached and candidates are addressed by immutable SHA/ref, avoiding a mutable branch as authority |

## Contract fields

Atomic Scope Mode is opt-in, so existing frozen contract JSON and hashes remain compatible. An atomic contract adds:

- `scope_mode`: `STRICT`, `BOUNDED`, or `EXPLORATORY`;
- `scope_owner` and positive `scope_revision`;
- `allowed_read_files`, while `allowed_files` remains the write boundary;
- `allowed_generated_files` and owner-approved `approved_scope_expansions`;
- optional `allowed_symbols` mapping a path to Python function/class names;
- `scope_exceptions` explaining approved expansion or exception decisions;
- `scope_write_enabled` for read-only exploratory tasks;
- optional `forbid_new_imports` for tasks that explicitly prohibit new Python imports.

STRICT mode accepts explicit files, not wildcard write paths. BOUNDED mode accepts narrow file/path patterns. EXPLORATORY mode may allow
broader investigation, but writes still require an explicit write boundary and independent review. An agent cannot alter the frozen
contract or approve its own expansion.

## Evidence and failure behavior

Validation evidence records task ID, baseline SHA, candidate SHA, effective scope policy, policy hash, normalized name-status entries,
line counts, special modes, binary paths, gate-configuration paths, worktree state, risk flags and disposition. An out-of-scope or dirty
candidate is marked `QUARANTINED`; StageMesh preserves its exact commit under a quarantine ref and integration remains blocked.

Changing an already active atomic policy uses the existing audited `rebind-contract` operator path. A reason is mandatory, prior evidence
is invalidated when the contract digest changes, and `task.scope_policy_revised` preserves the old/new policies and hashes. Hand-editing
the SQLite store is not an approval mechanism.

Known limits are explicit. Python symbol enforcement does not prove semantic intent. Non-Python symbol scope requires a future language
adapter or independent approval. Ignored files are observable in the task worktree but cannot be part of the candidate until project Git
policy admits them. Risk flags require review; they are not automatic proof that a large or binary change is wrong.
