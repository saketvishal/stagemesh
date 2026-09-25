#!/usr/bin/env bash
# Audits the public GitHub owner profile against docs/OWNER_PROFILE_CHECKLIST.md
# using only publicly readable data (no write access, no private scopes
# needed beyond an authenticated `gh`). Reports which checklist items already
# pass and which need a human to act. Does not modify the profile.
#
# Usage:
#   OWNER=saketvishal ./scripts/audit_owner_profile.sh
#
# Requires: gh CLI authenticated (`gh auth status`).
set -euo pipefail

OWNER="${OWNER:?Set OWNER=<github-username> before running}"

echo "==> Auditing public profile for ${OWNER}"
echo

profile_json=$(gh api "users/${OWNER}")
bio=$(jq -r '.bio // ""' <<<"${profile_json}")
blog=$(jq -r '.blog // ""' <<<"${profile_json}")
company=$(jq -r '.company // ""' <<<"${profile_json}")

echo "[Bio]"
if [[ -z "${bio}" ]]; then
  echo "  MISSING — no bio set. Human action required: add a concise technical bio."
else
  echo "  Present: \"${bio}\""
  if grep -qiE 'caventra' <<<"${bio}"; then
    echo "  FLAG: bio mentions Caventra — human action required: remove."
  fi
fi
echo

echo "[Company field]"
if grep -qiE 'caventra' <<<"${company}"; then
  echo "  FLAG: company field is \"${company}\" — mentions Caventra. Human action required: remove."
else
  echo "  OK (\"${company:-<empty>}\", no Caventra reference)"
fi
echo

echo "[Website/social link]"
if [[ -z "${blog}" ]]; then
  echo "  MISSING — no link set. Human action required: add a real, currently-maintained link, or leave blank if none exists."
else
  echo "  Present: ${blog}"
fi
echo

echo "[StageMesh pinned]"
pinned=$(gh api graphql -f query='
  query($login: String!) {
    user(login: $login) {
      pinnedItems(first: 10, types: [REPOSITORY]) {
        nodes { ... on Repository { name } }
      }
    }
  }' -f login="${OWNER}" --jq '.data.user.pinnedItems.nodes[].name' 2>/dev/null || true)
if grep -qiE '^stagemesh$' <<<"${pinned}"; then
  echo "  OK — stagemesh is pinned."
else
  echo "  MISSING — stagemesh is not among pinned repos (${pinned:-none pinned}). Human action required: pin it via \"Customize your pins\"."
fi
echo

echo "[Profile README]"
readme_repo="${OWNER}/${OWNER}"
if gh api "repos/${readme_repo}/readme" >/dev/null 2>&1; then
  readme_content=$(gh api "repos/${readme_repo}/readme" --jq '.content' | base64 -d 2>/dev/null || echo "")
  if grep -qiE 'caventra' <<<"${readme_content}"; then
    echo "  FLAG: profile README (${readme_repo}) mentions Caventra — human action required: remove."
  else
    echo "  OK — profile README exists, no Caventra reference found."
  fi
else
  echo "  No profile README repo (${readme_repo}) — not required."
fi
echo

echo "==> Audit complete. Any MISSING/FLAG line above is a human-only action;"
echo "    cross-check against docs/OWNER_PROFILE_CHECKLIST.md."
