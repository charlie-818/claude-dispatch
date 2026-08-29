#!/bin/bash
# Auto-deploy for CC Dispatch. Mirrors this checkout to origin/main and, when
# HEAD actually moves, restarts the ccdispatch service so a deploy target (e.g.
# Big Mac) always runs exactly what was pushed from the dev machine.
#
# Runs on the DEPLOY TARGET only. `git reset --hard` makes this checkout a pure
# mirror of origin — never edit code directly on the target, or it gets wiped.
# Runtime state (.sessions.json, vault, .token, etc.) is gitignored, so it is
# never touched by the reset.
set -euo pipefail
cd "$(dirname "$0")"

BRANCH="${DEPLOY_BRANCH:-main}"
SERVICE="com.charliebc.ccdispatch"

before="$(git rev-parse HEAD)"
git fetch --quiet origin "$BRANCH"
git reset --hard "origin/$BRANCH" >/dev/null
after="$(git rev-parse HEAD)"

if [ "$before" != "$after" ]; then
  echo "$(date '+%F %T') deploy ${before:0:8} -> ${after:0:8}, restarting $SERVICE"
  launchctl kickstart -k "gui/$(id -u)/$SERVICE"
fi
