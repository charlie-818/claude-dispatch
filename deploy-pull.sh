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

before="$(git rev-parse HEAD)"
git fetch --quiet origin "$BRANCH"
git reset --hard "origin/$BRANCH" >/dev/null
after="$(git rev-parse HEAD)"

if [ "$before" != "$after" ]; then
  echo "$(date '+%F %T') deploy ${before:0:8} -> ${after:0:8}, restarting server"
  # Restart in place: kill the running server and relaunch the SAME python
  # binary. iTerm2's Python API trusts connections by executable identity, so a
  # relaunch of ./.venv/bin/python inherits the existing authorization without a
  # GUI prompt (launchd supervision is avoided precisely because it would face
  # that prompt at boot with nobody to approve it).
  # cwd isn't in the process cmdline (it shows the resolved framework python +
  # "server.py"), so match on the script name. This host runs only this server.
  pkill -f "server.py" 2>/dev/null || true
  sleep 1
  nohup ./.venv/bin/python server.py >> server.out 2>&1 &
  disown 2>/dev/null || true
  sleep 3
  if lsof -iTCP:8788 -sTCP:LISTEN -n -P >/dev/null 2>&1; then
    echo "$(date '+%F %T') server back up on 8788"
  else
    echo "$(date '+%F %T') WARN server not listening on 8788 after relaunch"
  fi
fi
