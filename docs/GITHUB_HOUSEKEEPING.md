# GitHub Repository Housekeeping

This document is the public repository maintenance playbook for labels,
superseded issues, and stale pull requests. It is intentionally separate from
StageMesh runtime state: labels here help people read the public backlog, while
`stagemesh:*` labels mirror coordinator lifecycle state.

## Durable Label Taxonomy

Provision repository labels with:

```bash
stagemesh watcher provision-labels --repo OWNER/REPO
```

The managed taxonomy is deliberately small:

| Family | Labels | Use |
|---|---|---|
| Priority | `priority:P0`, `priority:P1`, `priority:P2` | Human planning priority, not runner dispatch state. |
| Type | `type:bug`, `type:docs`, `type:maintenance`, `type:feature` | The kind of work a contributor should expect. |
| Lifecycle | `lifecycle:needs-triage`, `lifecycle:superseded`, `lifecycle:validated` | Human backlog curation only. Do not mirror `READY`, `IN_PROGRESS`, `DONE`, or other coordinator states here. |
| Roadmap | `roadmap:future-work` | Accepted direction deferred beyond the current roadmap cut. |
| Contributor | `good first issue`, `help wanted` | Safe, well-scoped external contribution opportunities. |

Do not create public repository labels that duplicate StageMesh runtime state
unnecessarily. If an issue needs machine lifecycle visibility, use the existing
GitHub task-source `stagemesh:*` synchronization path. If an issue needs human
planning context, use the taxonomy above.

## Canonical Backlog Rule

Keep #53's canonical-backlog rule intact: the canonical capability issue remains
the source of truth for public backlog intent. Do not close or relabel that
canonical issue merely because related implementation exists in a local worktree.
Use integrated and validated evidence before calling work complete.

When a duplicate or older issue exists:

1. Identify the canonical capability issue or replacement PR.
2. Comment on the duplicate with a link to the canonical item.
3. Add `lifecycle:superseded`.
4. Close it only when the canonical replacement is public and clear enough that
   the old item would otherwise imply obsolete active work.
5. Preserve all historical discussion; do not delete issue or PR content.

When the relationship is uncertain, leave the item open with
`lifecycle:needs-triage` and link the suspected canonical item.

## Superseded Pull Requests

Close or mark a PR as superseded only when a canonical replacement exists, such
as a merged PR, an integrated issue, or a newer PR that carries the same
capability forward. Prefer a closing comment like:

```markdown
Superseded by #123, which is now the canonical path for this capability.
Preserving this PR for historical discussion.
```

Do not close a PR merely because a similar change exists locally. Require
integrated or otherwise validated evidence first.

## Public Backlog Hygiene

Public backlog views should not imply obsolete work is active:

- duplicates should link to the canonical item;
- superseded work should carry `lifecycle:superseded`;
- future ideas should carry `roadmap:future-work`;
- contributor-friendly work should carry both `good first issue` and
  `help wanted`, after passing the checklist in
  [GOOD_FIRST_ISSUES.md](GOOD_FIRST_ISSUES.md);
- validated completed work should carry `lifecycle:validated` only when
  integrated or externally verified evidence exists.
