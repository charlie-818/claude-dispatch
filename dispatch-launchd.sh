#!/bin/bash
# launchd wrapper for CC Dispatch. Ensures iTerm2 is running first (the server
# drives iTerm panes via iTerm2's Python API), then runs the server in the
# foreground so launchd KeepAlive can supervise it.
cd "$(dirname "$0")"
open -a iTerm 2>/dev/null   # no-op if already running
sleep 5                     # let iTerm2's API server come up on a cold boot
exec ./.venv/bin/python server.py
