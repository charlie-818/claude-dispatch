#!/bin/bash
# Launch CC Dispatch on BigMac. Run this INSIDE an iTerm2 window (it drives
# iTerm panes via iTerm2s Python API). Serves 127.0.0.1:8788, which
# `tailscale serve` publishes at https://<your-host>.<your-tailnet>.ts.net:8443
#
# Output goes to dispatch.log, not the window. If the window is closed the
# server's tty fds get revoked and every print() raises — which turned each
# /ws/fleet handshake into a 500 and the UI into "disconnected". File fds
# survive the window, so closing it is now harmless.
cd "$(dirname "$0")"
echo "CC Dispatch starting — output in $(pwd)/dispatch.log (safe to close this window)"
exec ./.venv/bin/python -u server.py >> dispatch.log 2>&1
