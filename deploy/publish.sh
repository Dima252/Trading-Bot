#!/usr/bin/env bash
# Regenerate the dashboard and publish it to GitHub Pages.
#
#   bash deploy/publish.sh
#
# GitHub Pages serves `docs/` on the default branch, so publishing is just a
# commit. No hosting to pay for, no port to expose, and -- the reason this is a
# separate script rather than a flag on `serve` -- no control surface reachable
# from the internet. The published page is the STATIC dashboard: it has no halt
# switch and no route that could reach an order.
#
# On a host this needs push credentials. A deploy key scoped to this one
# repository is the right shape; a personal access token with full account
# access is not.
set -euo pipefail

BOT_DIR="${BOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="${PY:-${BOT_DIR}/venv/bin/python}"
[ -x "${PY}" ] || PY="python"

cd "${BOT_DIR}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Rendering"
"${PY}" -m trading_bot dashboard --out docs/index.html

# Nothing to say if the numbers have not moved -- an empty commit every evening
# turns the history into noise.
if git diff --quiet -- docs/index.html; then
  echo "    unchanged; nothing to publish"
  exit 0
fi

say "Publishing"
git add docs/index.html
git -c user.name="trading-bot" \
    -c user.email="trading-bot@users.noreply.github.com" \
    commit -q -m "dashboard: $(date -u +%Y-%m-%d)"
git push -q origin HEAD

URL="https://$(git remote get-url origin \
  | sed -E 's#.*github.com[:/]([^/]+)/(.+)\.git#\1.github.io/\2#')/"
echo "    published to ${URL}"
