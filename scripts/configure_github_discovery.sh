#!/usr/bin/env bash
# Configures and verifies GitHub discovery metadata for the public StageMesh
# repository (topics, description, homepage, Discussions). Run by a
# maintainer with an authenticated `gh` CLI (repo admin scope) — this script
# makes live changes to a shared, public GitHub repository and is not
# executed automatically.
#
# Usage:
#   REPO=owner/stagemesh ./scripts/configure_github_discovery.sh apply
#   REPO=owner/stagemesh ./scripts/configure_github_discovery.sh verify
#   REPO=owner/stagemesh ./scripts/configure_github_discovery.sh          # apply then verify
#
# Requires: gh CLI authenticated (`gh auth status`) with admin rights on REPO.
set -euo pipefail

REPO="${REPO:?Set REPO=owner/name before running, e.g. REPO=saketvishal/stagemesh}"
MODE="${1:-all}"

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

apply() {
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
}

verify() {
  local failed=0

  echo "==> Verifying description on ${REPO}"
  local live_description
  live_description=$(gh api "repos/${REPO}" --jq .description)
  if [[ "${live_description}" == "${DESCRIPTION}" ]]; then
    echo "    OK"
  else
    echo "    MISMATCH: live description is: ${live_description}"
    failed=1
  fi

  echo "==> Verifying Discussions enabled on ${REPO}"
  local live_discussions
  live_discussions=$(gh api "repos/${REPO}" --jq .has_discussions)
  if [[ "${live_discussions}" == "true" ]]; then
    echo "    OK"
  else
    echo "    MISMATCH: has_discussions is ${live_discussions}"
    failed=1
  fi

  echo "==> Verifying topics on ${REPO}"
  local live_topics
  live_topics=$(gh api "repos/${REPO}/topics" -H "Accept: application/vnd.github+json" --jq '.names | sort | join(",")')
  local expected_topics
  expected_topics=$(printf '%s\n' "${TOPICS[@]}" | sort | paste -sd, -)
  if [[ "${live_topics}" == "${expected_topics}" ]]; then
    echo "    OK"
  else
    echo "    MISMATCH:"
    echo "      expected: ${expected_topics}"
    echo "      live:     ${live_topics}"
    failed=1
  fi

  echo "==> Verifying homepage on ${REPO}"
  local live_homepage
  live_homepage=$(gh api "repos/${REPO}" --jq .homepage)
  if [[ -z "${HOMEPAGE}" ]]; then
    echo "    (no expected homepage set; live value: ${live_homepage:-<empty>})"
  elif [[ "${live_homepage}" == "${HOMEPAGE}" ]]; then
    echo "    OK"
  else
    echo "    MISMATCH: expected ${HOMEPAGE}, live is ${live_homepage:-<empty>}"
    failed=1
  fi

  echo "==> Verifying social preview image on ${REPO}"
  local og_image
  og_image=$(curl -fsSL "https://github.com/${REPO}" | grep -o '<meta property="og:image" content="[^"]*"' | sed 's/.*content="//;s/"$//' || true)
  if [[ -n "${og_image}" && "${og_image}" != *"avatars"* && "${og_image}" != *"identicon"* ]]; then
    echo "    OK (og:image = ${og_image})"
    echo "    Manually confirm this is docs/design/social-preview.svg exported, not GitHub's default."
  else
    echo "    NOT SET: no custom social preview image detected (og:image = ${og_image:-<none>})."
    echo "    Upload docs/design/social-preview.svg (exported to PNG) via Settings -> General -> Social preview."
    failed=1
  fi

  if [[ "${failed}" -ne 0 ]]; then
    echo "==> verify FAILED — one or more items are not yet applied."
    return 1
  fi
  echo "==> verify PASSED — all automatable discovery metadata is live and correct."
}

case "${MODE}" in
  apply) apply ;;
  verify) verify ;;
  all) apply; echo; verify ;;
  *) echo "Usage: $0 [apply|verify]" >&2; exit 1 ;;
esac
