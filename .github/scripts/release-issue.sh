#!/usr/bin/env bash
# Post a Markdown file to this release's approval issue ("Release approval: <tag>"), creating it if needed.
# Usage: release-issue.sh <markdown-file>
set -euo pipefail
body_file="$1"
release="${GITHUB_REF_NAME}"
[ "${GITHUB_REF_TYPE}" = "tag" ] || release="${GITHUB_REF_NAME}@${GITHUB_SHA::7}"
title="Release approval: ${release}"
label="release-approval"
gh label create "$label" --color 0e8a16 --description "Agent release waiting for approval" 2>/dev/null || true
number=$(gh issue list --state open --label "$label" --search "\"$title\" in:title" --json number,title \
  --jq ".[] | select(.title == \"$title\") | .number" | head -n1)
head -c 60000 "$body_file" > "${body_file}.issue"
if [ -z "$number" ]; then
  gh issue create --title "$title" --label "$label" --body-file "${body_file}.issue"
else
  gh issue comment "$number" --body-file "${body_file}.issue"
fi
