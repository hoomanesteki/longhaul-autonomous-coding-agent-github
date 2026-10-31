#!/usr/bin/env bash
# Protect main and dev: PRs required, the `ci` check required and up to date,
# no force pushes, no deletions. Run once by the repo owner (needs admin rights).
# Usage: scripts/protect_branches.sh [OWNER/REPO]
#
# enforce_admins is false on purpose. The owner merges the phase-gate pull requests and the
# maintainer pushes dev as the admin, so GitHub has to let admins through. These rules bind
# collaborators and bots, not admins: a direct push to dev is rejected for a token without
# admin rights and accepted for an admin. See the CI section of docs/runbook.md.
set -euo pipefail

REPO="${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"

for B in main dev; do
  echo "Protecting ${REPO}@${B}"
  gh api -X PUT "repos/${REPO}/branches/${B}/protection" --input - >/dev/null <<'JSON'
{
  "required_status_checks": {"strict": true, "contexts": ["ci"]},
  "enforce_admins": false,
  "required_pull_request_reviews": {"required_approving_review_count": 0},
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
  echo "  required check: ci (strict)"
  echo "  pull request required, 0 approvals"
  echo "  force pushes and deletions blocked, admins not bound (enforce_admins is false)"
done

echo "Done."
