#!/usr/bin/env bash
# Configures GitHub discovery metadata for the public StageMesh repository
# (topics, description, homepage, Discussions). Run by a maintainer with an
# authenticated `gh` CLI (repo admin scope) — this script makes live changes
# to a shared, public GitHub repository and is not executed automatically.
#
# Usage:
#   REPO=owner/stagemesh ./scripts/configure_github_discovery.sh
#
# Requires: gh CLI authenticated (`gh auth status`) with admin rights on REPO.
set -euo pipefail

REPO="${REPO:?Set REPO=owner/name before running, e.g. REPO=saketvishal/stagemesh}"

DESCRIPTION="Provider-neutral control plane for multi-agent software engineering with durable stages, independent review, and recovery."

# Only set if a real, maintained StageMesh destination exists. Leave unset
# (do not invent a URL) if there is none yet.
HOMEPAGE="${HOMEPAGE:-}"

TOPICS=(
  multi-agent
  ai-agents
  claude-code
  codex
  orchestration
  developer-tools
  python
  cli
  software-engineering
  code-review
)

echo "==> Setting description on ${REPO}"
gh repo edit "${REPO}" --description "${DESCRIPTION}"

if [[ -n "${HOMEPAGE}" ]]; then
  echo "==> Setting homepage on ${REPO} to ${HOMEPAGE}"
  gh repo edit "${REPO}" --homepage "${HOMEPAGE}"
else
  echo "==> Skipping homepage (no real maintained URL provided via \$HOMEPAGE)"
fi

echo "==> Replacing topics on ${REPO}"
TOPICS_JSON=$(printf '"%s",' "${TOPICS[@]}")
TOPICS_JSON="[${TOPICS_JSON%,}]"
echo "{\"names\": ${TOPICS_JSON}}" | gh api -X PUT "repos/${REPO}/topics" \
  -H "Accept: application/vnd.github+json" --input -

echo "==> Enabling Discussions on ${REPO}"
gh api -X PATCH "repos/${REPO}" -f has_discussions=true >/dev/null

echo "==> Done. Social preview image cannot be set via the GitHub API;"
echo "    upload docs/design/social-preview.svg (exported to PNG) manually:"
echo "    Settings -> General -> Social preview -> Edit -> Upload an image."
