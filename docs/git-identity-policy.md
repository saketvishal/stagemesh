# Git identity and contributor attribution policy

GitHub credits a commit to an account from the commit's author/committer **email** (for `ID+login@users.noreply.github.com` the numeric
**ID** decides, not the login) and from `Co-authored-by` trailers. A wrong email or trailer therefore puts a stranger or a tool on the
repository's contributor list. This document records how that happened here and the rules that prevent it.

## What went wrong

| Symptom | Cause |
|---|---|
| `shubh2294` credited on `saketvishal/stagemesh` | Agent sessions invented the noreply address `12345678+saketvishal@users.noreply.github.com` (they set `GIT_AUTHOR_EMAIL` / `GIT_COMMITTER_EMAIL` or `user.email` by guesswork). `12345678` is the real numeric id of the account `shubh2294`, so GitHub attributes those commits to it. No StageMesh source contains the value. Commits `76e21c5` and `82bf2ab` carry the exact (resolving) form; ten more carry a malformed form with the name glued into the address, which resolves to no account. All are on the published branch `feat/report-export`. |
| `Claude` credited as co-author | The coding agent's default commit attribution appends `Co-Authored-By: Claude … <noreply@anthropic.com>`. 64 such trailers exist on the published branches `feat/report-export` and `feat/founder-hands-off-gate`. |
| `StageMesh <stagemesh@example.invalid>` and similar identities in history | StageMesh ran `git config user.email/user.name` inside every task worktree. **A worktree shares the repository's config**, so each call silently replaced the owner's identity in their real checkout. Provider commits were also authored as `StageMesh <provider> worker …@stagemesh.invalid`, and GitHub squash merges turned those authors into `Co-authored-by` trailers. |

`origin/main` contains neither the placeholder id nor a Claude trailer; its contributor list shows only `saketvishal` (plus
`stagemesh@example.invalid` / `grok+local-worker@stagemesh.invalid` as non-account "anonymous" entries).

## Rules (enforced in code and tests)

1. **StageMesh never writes `user.name` / `user.email`** to any git config, global, local or worktree. A source-scan test fails the build
   if it does. Worktree creation, fencing and `init_if_needed` no longer touch identity.
2. **Identity is resolved, not configured** (`stagemesh/git_identity.py`). Commits, rebases, merges, cherry-picks, stashes and tags run
   by `GitWorkspace` get the owner's identity (environment, then git config) supplied for that one command. Only if no identity exists
   anywhere is the synthetic `StageMesh <stagemesh@stagemesh.invalid>` used, and still only per command, never persisted.
3. **Placeholder and malformed identities are refused**: noreply ids in `PLACEHOLDER_NOREPLY_IDS` (currently `12345678`), and emails
   containing whitespace or angle brackets, raise `GitIdentityError` before any commit is created.
4. **AI providers are execution metadata, not contributors.** Provider-produced candidate commits carry the project identity;
   the provider is recorded in StageMesh's candidate/execution records (`produced_by`, `actor`).
5. **No tool trailers.** `Co-authored-by` lines naming AI providers, `*.invalid` addresses, `+local-worker` addresses or placeholder ids
   are stripped from messages StageMesh writes. Trailers for real people are kept. A coding agent that commits directly (not through
   StageMesh) adds its own trailer unless its tooling is configured not to; for Claude Code that is the `attribution` setting
   (`{"commit": "", "pr": "", "sessionUrl": false}`) in the user's or project's settings. This repository sets it in `.claude/settings.json`.
   `scripts/attribution_tool.py audit` detects any such trailer before it is published.
6. Genuine third-party authorship is never rewritten: rebases keep the original author.

## Auditing and correcting history

```
python scripts/attribution_tool.py audit --repo . origin/main origin/feat/report-export     # exit 1 on any offence
python scripts/attribution_tool.py rewrite --source . --work <new empty dir> \
    --owner-name "Vishal Singh" --owner-email "20689561+saketvishal@users.noreply.github.com" --owner-login saketvishal \
    --ref refs/remotes/origin/feat/report-export …
```

`rewrite` works in a mirror clone and never touches the source or any remote. A commit changes only if it is an offender or descends
from one; every other commit keeps its exact hash and signature. Trees, authors, dates and subjects are never altered. Publishing the
corrected refs (force-push) is a separate, explicitly authorized step.
