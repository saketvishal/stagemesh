# Enforce contributor attribution

Claude settings remain disabled in `.claude/settings.json`. They are a prevention preference, not a Git publication boundary.

## Required GitHub configuration

After this workflow is merged into `main`, an administrator must create or update an active branch ruleset targeting `main`:

- Require a pull request before merging; preserve any existing review requirements.
- Require status check **Attribution policy**, sourced from GitHub Actions. Run a clean follow-up PR first if GitHub needs the check to appear in its selector.
- Require branches to be up to date (or use the merge queue; this workflow also handles `merge_group`).
- Block force pushes and branch deletion.
- Remove routine administrator, user and app bypass entries. Keep any emergency access deliberate and separate from coding-agent credentials.
- Retain other required checks. Do not replace the existing protection configuration with attribution alone.

The workflow uses `pull_request_target`, reads the checker from the exact trusted base commit, and fetches the candidate only as Git objects. It never checks out or executes candidate files, installs candidate dependencies, or grants a write token. It has no path filters: documentation-only and configuration-only PRs are checked too. Every commit reachable from the candidate but absent from the base is inspected, including merge-side commits. Existing baseline offences do not block unrelated fixes.

Treat edits to this workflow, `scripts/check_attribution.py`, and `src/stagemesh/git_identity.py` as security-relevant policy changes and review them before merging. A repository administrator can still change the policy; no repository setting can prevent that administrator from deliberately disabling protection.

The initial PR cannot prove the new target workflow on GitHub until it exists on the default branch. Verify a clean follow-up PR, then require the exact check above. The negative tests use disposable local Git repositories, so testing does not publish an unwanted contributor again. GitHub refusal of a merge remains unverified until an administrator enables the rule.

## Optional local installation

From each operational clone, run:

```powershell
python scripts/install_attribution_hooks.py
```

This installs `commit-msg` and `pre-push` checks. It refuses to overwrite existing hooks or an existing `core.hooksPath`; incorporate the printed checker commands into those hooks instead. Hooks resolve the active worktree at execution time. The Python executable used for installation must remain available. Local hooks can be bypassed and do not replace GitHub protection.

For automation, validate an incoming range before publication:

```powershell
python scripts/check_attribution.py --base <full-base-sha> --head <full-candidate-sha>
```

Exit 0 means accepted; 1 means an attribution violation; 2 means validation could not complete. Treat both nonzero results as a publication failure. The publication checker reuses StageMesh's identity policy and additionally rejects synthetic identities at the publication boundary. Internal unconfigured lab repositories retain their existing synthetic fallback; they still must use an approved human identity before publication.

## Historical correction is separate

Six October 12 commits on `main` contain Claude trailers despite the committed settings. Preventing new violations does not remove those trailers or immediately change the Contributors sidebar. Inventory live branches and tags, freeze publication, prepare a correction in a separate mirror, and compare every original/corrected commit's tree, human authorship, dates, and parent mapping. Preserve the original refs and commit map privately before publishing any correction. Recheck remote heads immediately before any approved force-with-lease updates. Coordinate existing open PRs and clones; never blindly rewrite active branches or release tags.

After correction, if GitHub still displays stale contributors beyond its refresh period, the owner should ask GitHub Support to rebuild contributor statistics and inspect retained PR refs. A support request must distinguish verified current-history offences from suspected stale display data.
