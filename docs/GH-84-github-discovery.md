# GitHub discovery metadata (GH-84)

Configures the public repository's discoverability: topics, description,
homepage, Discussions, and social preview. Repository-level settings and
profile settings aren't part of the git tree, so this is split into what an
agent can prepare as files vs. what a human maintainer must apply.

## What's here

- `scripts/configure_github_discovery.sh` — idempotent script (uses the
  `gh` CLI) that a maintainer with repo admin rights runs to set the
  description, topics, and enable Discussions, then **verify** each one
  against the live repository (`apply`, `verify`, or both — see "How to
  apply" below). It is not run automatically from this worktree: this
  environment has no authenticated `gh` session and making live changes to
  a shared public repository is a visible, hard-to-reverse action that
  requires an explicit maintainer decision, not something an agent should
  do unattended.
- `docs/design/social-preview.svg` + `docs/design/social-preview.md` — the
  social preview card design and upload steps (GitHub has no API for
  *setting* the social preview image, so upload is manual regardless; the
  `verify` step above checks the live `og:image` meta tag to confirm
  something other than GitHub's default has been uploaded).
- `scripts/audit_owner_profile.sh` — reads the live public profile (bio,
  company field, website link, pinned repos, profile README) via `gh api`
  and reports exactly which items in `docs/OWNER_PROFILE_CHECKLIST.md`
  still need human action, instead of leaving the whole checklist generic.
- `docs/OWNER_PROFILE_CHECKLIST.md` — human-only owner profile steps (bio,
  pinning, link, privacy check); use `audit_owner_profile.sh` to check
  current state first.

## Repository topics

```
multi-agent, ai-agents, claude-code, codex, orchestration,
developer-tools, python, cli, software-engineering, code-review
```

## Repository description

```
Provider-neutral control plane for multi-agent software engineering with durable stages, independent review, and recovery.
```

This replaces generic "autonomous multi-agent workflows" phrasing with the
control-plane/stage-machine positioning, matching the language already used
in `README.md` ("Stages belong to the coordinator. Agents are replaceable
executors.").

## Homepage

Not set. No separate maintained StageMesh destination (docs site, landing
page) exists yet beyond the repository itself — inventing one would violate
the "real destination only" requirement. Revisit when one exists.

## How to apply

```
gh auth login   # if not already authenticated, needs repo admin scope

# Set description/topics/Discussions and verify they took effect:
REPO=owner/stagemesh ./scripts/configure_github_discovery.sh apply
REPO=owner/stagemesh ./scripts/configure_github_discovery.sh verify

# Or both in one step:
REPO=owner/stagemesh ./scripts/configure_github_discovery.sh
```

`verify` re-reads the live repository via `gh api` and fails (non-zero exit)
if the description, topics, Discussions flag, homepage, or social preview
image don't match what was applied — so applying and confirming are one
command, not a documentation claim.

Then upload the social preview PNG (`docs/design/social-preview.md`;
`verify` above will detect once it's live) and run the owner profile audit:

```
OWNER=<github-username> ./scripts/audit_owner_profile.sh
```

which reports the exact remaining human-only steps against
`docs/OWNER_PROFILE_CHECKLIST.md`.

### Why this isn't applied from this worktree

This task was implemented in an isolated build-agent worktree with no
authenticated `gh` session and no credentials for the public StageMesh
GitHub repository. Even if credentials were present, editing a shared
public repository's live settings and a GitHub account's public profile
are visible, hard-to-reverse actions — they require a maintainer's
explicit, authenticated action, not unattended execution by an agent. The
scripts above are the complete, ready-to-run mechanism for a maintainer to
apply and verify every automatable item in one pass.
