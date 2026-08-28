#!/usr/bin/env bash
# CC Dispatch — write a pane's working/idle state for the fleet view.
#
# The status JSON tells the server WHAT a pane is; this tells it whether the
# pane is mid-turn. The server only distinguishes "working" vs "idle", and uses
# the state file's mtime as the "in this state since" clock (so the phone timer
# survives a server restart).
#
# Wire to Claude Code hooks (see examples/settings.snippet.json):
#   UserPromptSubmit -> working     Stop / Notification -> idle
#
# Usage (from a hook): cc-state.sh working|idle
set -euo pipefail

STATE="${1:-idle}"
FLEET_DIR="${CC_FLEET_DIR:-/tmp/cc-status}"
mkdir -p "$FLEET_DIR"

# Hook JSON on stdin carries session_id; match the statusline's <sid>.json name.
SID="$(python3 -c 'import json,sys
try: print((json.load(sys.stdin) or {}).get("session_id",""))
except Exception: print("")' 2>/dev/null || true)"

[ -n "$SID" ] || exit 0
printf '%s' "$STATE" > "$FLEET_DIR/$SID.state"
