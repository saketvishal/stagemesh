# Contributor-attribution audit (2026-10-10)

Scope: every ref on the remote (51 branches, 1 tag, 72 `refs/pull/*/head`) plus local refs, 1,420 commits. Reachability is from `origin/main` at `1e452f0`.

## Result

- **`origin/main` is clean**: 0 commits with a `claude`/`anthropic.com` identity or trailer, 0 with the placeholder id `12345678`. Its GitHub contributor API lists only `saketvishal` (288) plus non-account email-only entries (`stagemesh@example.invalid`, `*+local-worker@stagemesh.invalid`).
- **71 offending commits exist; 58 are still hosted by GitHub, all outside `main`**: reachable only through `refs/pull/*/head` of PRs #137-#140 and #161-#169. 13 more exist only in local backup/quarantine refs and were never published.
- **`shubh2294`**: 2 commits (`82bf2ab`, `76e21c5`) use the exact noreply form `12345678+saketvishal@users.noreply.github.com`. GitHub resolves `ID+login` by the numeric ID, and `12345678` is the account `shubh2294`; the GitHub commits API reports `author.login = committer.login = shubh2294` for both. 10 more hosted commits carry a malformed form (`Vishal Singh 12345678+...`) that resolves to no account.
- **`claude`**: 49 commits carry `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` (the coding agent's default attribution). GitHub credits `noreply@anthropic.com` to the `claude` account.
- **Why the UI can still list them**: `main` was rewritten on or before 2026-10-09 (backup refs `refs/backup/attribution-20261009/*`). The original merge commits of the PRs above (e.g. #137 `bb51bf4`, #161 `141d756`, #169 `0fe8575`) are no longer on `main`, but GitHub still stores them and the PR heads. The contributors graph is cached and computed from the history it saw earlier; the REST contributors endpoint, which reflects current `main`, no longer lists either account. This cannot be confirmed from the UI here; see decision below.

## Where each bad attribution came from

| Contributor shown | Mechanism | Commits (hosted / local-only) | On main |
|---|---|---|---|
| `shubh2294` | agent sessions invented the noreply address `12345678+saketvishal@...`; `12345678` is that account's real id | 12 / 0 | no |
| `claude` | the coding agent's default `Co-Authored-By` trailer | 49 / 13 | no |
| `saketvishal` | the real owner | n/a | yes |

Not contributors, informational: `StageMesh <stagemesh@example.invalid>`, `StageMesh <provider> worker <provider+local-worker@stagemesh.invalid>` (provider-named authors) exist on `main` (45 commits with `.invalid` or worker addresses) and branches; they match no GitHub account and appear only as anonymous entries.

## Commits still hosted by GitHub (not on `main`)

| SHA | Kind | Author | Committer | GitHub login (author / committer) | PR head refs |
|---|---|---|---|---|---|
| `033444c146` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `06a49b7294` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `0a9ec9d6a8` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | #169 |
| `0b77eac9cb` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `141d756661` | claude-trailer | saketvishal <20689561+saketvishal@users.noreply.github.com> | GitHub <noreply@github.com> | saketvishal / web-flow | #162, #163 |
| `1be25be37d` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `1f3410706e` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `2f5f6aeb68` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `32ff06c9b0` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `348301df88` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `3907ca6941` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `39679bd2cc` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `428e63a466` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | #164, #165, #166, #167, #168, #169 |
| `43543d3cf8` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `4916e9fb9a` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | #164, #165, #166, #167, #168, #169 |
| `4b1a32004c` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | #168 |
| `4d08acc8ab` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | #165, #166, #167, #168, #169 |
| `5755c427d2` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `59480507fb` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `604afd3fef` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `6096ce4317` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `649bf5f47c` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `657b8e9bd4` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `6c32a791f0` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `70e5cd227a` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `73ed7ba5af` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `782a87e3cc` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `7cb270a878` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `7d36502101` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | #169 |
| `86a01c89f9` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `86b23ac5e8` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `88685bdbb0` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `8933c0b30a` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `8a1a7cce90` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `8eadf8b094` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | #169 |
| `902f43c8c1` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | #169 |
| `a737df2240` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `af69063051` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | #165, #166, #167, #168, #169 |
| `b7f809dcb6` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `bd084a526d` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `c57353b605` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | #169 |
| `cb526a6024` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `d0ce4ffbd9` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | - |
| `dd92e44628` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `e20028ab3e` | claude-trailer | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | saketvishal / saketvishal | #167, #168, #169 |
| `ee25214718` | claude-trailer | StageMesh <stagemesh@example.invalid> | StageMesh <stagemesh@example.invalid> | - / - | #161, #162, #163 |
| `1636d812fe` | numeric-id-12345678 | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | - / - | #137, #138, #139, #140, #161, #162, #163 |
| `1ceff6aaf6` | numeric-id-12345678 | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | - / - | #137, #138, #139, #140, #161, #162, #163 |
| `76e21c51c9` | numeric-id-12345678 | Vishal Singh <12345678+saketvishal@users.noreply.github.com> | Vishal Singh <12345678+saketvishal@users.noreply.github.com> | shubh2294 / shubh2294 | #139, #140, #161, #162, #163 |
| `7cb39dd257` | numeric-id-12345678 | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | - / - | #137, #138, #139, #140, #161, #162, #163 |
| `82bf2abc23` | numeric-id-12345678 | Vishal Singh <12345678+saketvishal@users.noreply.github.com> | Vishal Singh <12345678+saketvishal@users.noreply.github.com> | shubh2294 / shubh2294 | #140, #161, #162, #163 |
| `977624a742` | numeric-id-12345678 | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | - / - | #137, #138, #139, #140, #161, #162, #163 |
| `9aea7264fe` | numeric-id-12345678 | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | - / - | #137, #138, #139, #140, #161, #162, #163 |
| `ec014a86ad` | numeric-id-12345678 | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | - / - | #137, #138, #139, #140, #161, #162, #163 |
| `ee1e3181c1` | numeric-id-12345678 | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | - / - | #137, #138, #139, #140, #161, #162, #163 |
| `636eda1f04` | numeric-id-12345678, claude-trailer | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | - / - | #161, #162, #163 |
| `6c8286d7cd` | numeric-id-12345678, claude-trailer | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | - / - | #161, #162, #163 |
| `7187eebaa3` | numeric-id-12345678, claude-trailer | Vishal Singh <Vishal Singh 12345678+saketvishal@users.noreply.github.com> | Vishal Singh <20689561+saketvishal@users.noreply.github.com> | - / saketvishal | #164, #165, #166, #167, #168, #169 |

## Local-only (never published)

`ea444ccc96`, `ae3c92a17d`, `79dac74ca3`, `9c72cce2ed`, `7f75f3a549`, `4999da8cfc`, `7092e0541b`, `f337ab6176`, `b08929e4c4`, `d1a1d9be33`, `31eeb83829`, `9f062d9fb0`, `228b2e1bf6`  
Held by local `refs/heads/integration/p0-candidate-isolation-and-review-reliability`, `fix/candidate-baseline-isolation`, `fix/review-reliability`, `refs/backup/attribution-20261009/*` and `refs/stagemesh/quarantine/*`.

## Forward fix (this PR)

Already in place from #258: identity resolved never written, per-command commit identity, placeholder-id refusal, Co-authored-by stripping, `scripts/attribution_tool.py`, `.claude/settings.json` attribution off. Added here:
- provider subprocesses (implementation and review) launched with the project identity pinned in `GIT_AUTHOR_*`/`GIT_COMMITTER_*`;
- integration gate: a candidate whose own commits (`baseline..candidate`) credit an AI provider, a StageMesh worker or a placeholder id is refused with `attribution_violation`;
- `validate_identity` refuses AI-provider and `+local-worker` addresses before any commit;
- `doctor` reports the resolved identity and warns; `queue-run`/`continue` preflight fails on an unapproved identity.

## Cleanup options (owner decision; nothing was rewritten)

1. **Leave history, prevent future leaks (recommended).** `main` is already clean. Cost: GitHub keeps the old objects reachable via `refs/pull/*`, so the contributors graph may keep showing the two accounts until GitHub recomputes it. Risk: none to the repo. First step: open the Insights > Contributors graph after a day; if it still lists them, go to 2.
2. **GitHub Support cache/object purge.** Ask GitHub Support (sensitive-data / contributor-attribution removal) to purge cached views and unreferenced objects for the listed SHAs and to garbage-collect `refs/pull/*`. Cost: support turnaround. Risk: low; no history change. `refs/pull/*` cannot be deleted by the owner.
3. **One-time rewrite of the published branches that still carry the commits.** Not needed for `main` (already clean). Rewriting PR branches would not remove `refs/pull/*` objects, so it does not fix the display by itself. Risk: breaks open clones and PR links.
4. **Recreate the repository.** Removes every old object and all `refs/pull/*`, at the cost of issues, PRs, stars and links (or a manual migration). Only justified if option 2 fails and the display matters more than history.

