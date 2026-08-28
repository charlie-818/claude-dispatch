#!/usr/bin/env bash
# CC Dispatch — Claude Code statusLine hook.
#
# Claude Code pipes a JSON status blob on stdin every time it repaints the
# status line. This script:
#   1. tags that blob with the pane's iTerm2 session id (so the dispatch server
#      can map a status file back to the actual pane it can send keys to),
#   2. writes it atomically into CC_FLEET_DIR for the server to read, and
#   3. prints a one-line status back for the terminal itself.
#
# Install: copy to ~/.claude/statusline.sh, chmod +x, and point settings.json
# "statusLine" at it (see examples/settings.snippet.json).
set -euo pipefail

FLEET_DIR="${CC_FLEET_DIR:-/tmp/cc-status}"
mkdir -p "$FLEET_DIR"

# Capture Claude Code's JSON from stdin BEFORE running python — a heredoc would
# otherwise take over python's stdin and the blob would be lost.
INPUT="$(cat)"

printf '%s' "$INPUT" | python3 -c '
import json, os, sys, pathlib

fleet_dir, iterm_pane = sys.argv[1], sys.argv[2]
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}

# The server keys panes off iterm_pane ("wNtNpN:GUID" -> GUID). No id, no pane.
if iterm_pane:
    d["iterm_pane"] = iterm_pane

sid = d.get("session_id") or "unknown"
out = pathlib.Path(fleet_dir) / (sid + ".json")
tmp = out.with_suffix(".json.tmp")
tmp.write_text(json.dumps(d))
os.replace(tmp, out)                      # atomic swap; readers never see a partial file

# one-line status for the terminal
model = (d.get("model") or {}).get("display_name", "claude")
cwd   = (d.get("workspace") or {}).get("current_dir") or d.get("cwd") or ""
cost  = (d.get("cost") or {}).get("total_cost_usd")
bits  = [model]
if cwd:
    bits.append(os.path.basename(cwd.rstrip("/")) or cwd)
if cost:
    bits.append("$%.2f" % cost)
print(" · ".join(str(b) for b in bits))
' "$FLEET_DIR" "${ITERM_SESSION_ID:-}"
