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
  changed="$(git diff --name-only "$before" "$after")"
  echo "$(date '+%F %T') deploy ${before:0:8} -> ${after:0:8}"

  # Frontend (static/*, index.html) is read off disk per request — the pull alone
  # deploys it live, no restart. Only a Python change needs the server reloaded.
  if echo "$changed" | grep -qE '\.py$'; then
    # Hot-reload in place: SIGHUP makes the LIVE server os.execv itself, so the
    # new image inherits the running process's ITERM2_COOKIE and reconnects to
    # iTerm2 with no GUI trust prompt. An external relaunch (pkill + venv python)
    # can't do this — it has no iTerm2 session — which is why past deploys logged
    # "not listening". The pid is stable across execv (same process).
    pid="$(cat .server.pid 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "$(date '+%F %T') py change — SIGHUP reload pid $pid"
      kill -HUP "$pid"
      sleep 4
      if lsof -iTCP:8788 -sTCP:LISTEN -n -P >/dev/null 2>&1; then
        echo "$(date '+%F %T') server reloaded, listening on 8788"
      else
        echo "$(date '+%F %T') WARN server not listening on 8788 after HUP"
      fi
    else
      echo "$(date '+%F %T') WARN no live server pid (.server.pid stale/missing);" \
           "py change is on disk but needs a manual start from inside iTerm2"
    fi
  else
    echo "$(date '+%F %T') frontend-only — served fresh from disk, no reload"
  fi
fi
