#!/bin/bash
# Launch CC Dispatch on BigMac. Run this INSIDE an iTerm2 window (it drives
# iTerm panes via iTerm2s Python API). Serves 127.0.0.1:8788, which
# `tailscale serve` publishes at https://<your-host>.<your-tailnet>.ts.net:8443
cd "$(dirname "$0")"
exec ./.venv/bin/python server.py
