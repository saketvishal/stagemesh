# GitHub discovery metadata (GH-84)

Configures the public repository's discoverability: topics, description,
homepage, Discussions, and social preview. Repository-level settings and
profile settings aren't part of the git tree, so this is split into what an
agent can prepare as files vs. what a human maintainer must apply.

## What's here

- `scripts/configure_github_discovery.sh` — idempotent script (uses the
  `gh` CLI) that a maintainer with repo admin rights runs to set the
  description, topics, and enable Discussions. It is not run automatically;
  it makes live changes to a shared public repository.
- `docs/design/social-preview.svg` + `docs/design/social-preview.md` — the
  social preview card design and upload steps (GitHub has no API for this,
  so it's manual regardless).
- `docs/OWNER_PROFILE_CHECKLIST.md` — human-only owner profile steps (bio,
  pinning, link, privacy check).

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
REPO=owner/stagemesh gh auth login   # if not already authenticated
REPO=owner/stagemesh ./scripts/configure_github_discovery.sh
```

Then complete the social preview upload (`docs/design/social-preview.md`)
and the owner profile checklist (`docs/OWNER_PROFILE_CHECKLIST.md`).
