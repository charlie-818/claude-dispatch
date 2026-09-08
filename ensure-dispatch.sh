#!/bin/bash
# Keep CC Dispatch alive across reboots and crashes.
#
# The server drives iTerm2's Python API and only connects when it is launched
# from a real iTerm2 session (it needs ITERM2_COOKIE). Launching it headless —
# from launchd or over ssh — hangs forever at the API connect. So this script
# never runs the server itself: it opens iTerm2 and asks iTerm2 to run
# start-dispatch.sh in a fresh window.
#
# Idempotent: if something is already listening on 8788 it does nothing, so it
# is safe to run at login and on a short interval.
cd "$(dirname "$0")"

PORT=8788
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

listening() { lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; }

if listening; then
  exit 0
fi

log "8788 idle — starting dispatch"

if ! pgrep -x iTerm2 >/dev/null; then
  log "opening iTerm2"
  open -a iTerm || { log "open -a iTerm failed"; exit 1; }
fi

# Wait for iTerm2's API server to be answering AppleScript before driving it.
for _ in $(seq 1 30); do
  osascript -e 'tell application "iTerm2" to count windows' >/dev/null 2>&1 && break
  sleep 1
done

# A stale server can be parked on a hung API connect, holding nothing but the
# pid file. Clear that one process out — matched by pid file, then confirmed by
# its command line, since a bare `pkill -f server.py` also kills shells whose
# cmdline merely mentions it.
stale=$(cat .server.pid 2>/dev/null)
if [ -n "$stale" ] && ps -p "$stale" -o command= 2>/dev/null | grep -q 'server\.py'; then
  log "killing stale server pid $stale"
  kill "$stale" 2>/dev/null
  sleep 2
fi

osascript <<'OSA' 2>&1 | sed 's/^/osascript: /'
tell application "iTerm2"
  set w to (create window with default profile)
  tell current session of w to write text "cd ~/claude-dispatch && ./start-dispatch.sh"
end tell
OSA

for _ in $(seq 1 30); do
  sleep 1
  if listening; then log "dispatch up on $PORT"; exit 0; fi
done

log "dispatch did NOT come up on $PORT within 30s"
exit 1
