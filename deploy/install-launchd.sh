#!/bin/bash
# Install launchd agent for CC Dispatch
#
# Usage: install-launchd.sh [ensure|server|deploy|all]
# Default: ensure
#
# Jobs:
#   ensure  - Recommended. Watchdog that opens iTerm2 and starts the server inside it.
#             Required because the server needs ITERM2_COOKIE and hangs when launched headless.
#   server  - Runs server directly under launchd KeepAlive (only works if iTerm2 grants
#             API access to launchd-spawned processes).
#   deploy  - Origin/main auto-mirror for a deploy target. Runs git reset --hard.
#   all     - Install all three jobs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

MODE="${1:-ensure}"

install_agent() {
  local name="$1"
  local template="$SCRIPT_DIR/launchd/$name.plist"
  local dest="$HOME/Library/LaunchAgents/$name.plist"

  if [[ ! -f "$template" ]]; then
    echo "Error: Template not found: $template" >&2
    return 1
  fi

  mkdir -p "$(dirname "$dest")"
  sed "s|__REPO__|$REPO_DIR|g" "$template" > "$dest"

  launchctl unload "$dest" 2>/dev/null || true
  launchctl load "$dest"
  echo "Installed: $name"
}

case "$MODE" in
  ensure)
    install_agent "com.ccdispatch.ensure"
    ;;
  server)
    install_agent "com.ccdispatch"
    ;;
  deploy)
    install_agent "com.ccdispatch.deploy"
    ;;
  all)
    install_agent "com.ccdispatch.ensure"
    install_agent "com.ccdispatch"
    install_agent "com.ccdispatch.deploy"
    ;;
  *)
    echo "Usage: $0 [ensure|server|deploy|all]" >&2
    exit 1
    ;;
esac
