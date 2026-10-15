# Attribution enforcement activation record — October 15, 2026

PR #358 merged as 644024851fa38960f4602030147b19cf4f00584a after both Linux and Windows CI jobs passed. The publication checker rejects AI-provider, synthetic worker and known placeholder attribution while preserving human co-authors and internal lab fallback behavior.

This documentation-only PR exercises the trusted-base `Attribution policy` workflow now present on main. Its result is a positive workflow check; local negative tests verify rejection without publishing an unwanted contributor.

## Administrator action still required

Inspected repository ruleset 23915766 (`protect main branch`):
- Enforcement is active, but its branch include and exclude lists are both empty.
- It requires four `Test (... Python ...)` check names, while current CI reports jobs named `windows` and `linux`.
- Its bypass list is empty.
- The connected GitHub app cannot access branch-protection administration (HTTP 403).

Open https://github.com/saketvishal/stagemesh/rules/23915766 and edit the repository ruleset in Settings:
1. Set the target explicitly to `main` (or the default branch).
2. Require pull requests, block force pushes and deletion, and keep routine agent bypasses disabled.
3. Reconcile the four old required-check names with the workflows currently in use. For the current StageMesh CI workflow, select `windows` and `linux` from GitHub Actions; retain any other checks actually required by active workflows.
4. Add the exact GitHub Actions check `Attribution policy` after this verification PR runs. Require up-to-date branches or use the merge queue.
5. Confirm the resulting effective main rules include attribution enforcement. A passing workflow by itself does not prove that a failing workflow blocks merging.

Do not label enforcement complete until the effective rule and merge refusal have been verified. Review future changes to the workflow and checker as policy changes.

## Historical cleanup status

An offline correction of main at 0a3214aa927a566e4d05e84d0072a524c7527e6a removed six Claude trailers. All 459 commit trees, human identities, timestamps, subjects and parent mappings were verified; 46 hashes changed and no narrow attribution offences remained.

The offline candidate predates PR #358 and must never replace current main directly. Publication requires a fresh live-ref inventory and a new correction containing all current work, coordinated with active PRs and clones. Remote history and release tags have not been rewritten. GitHub Support may still be needed to refresh stale contributor displays after historical correction.
