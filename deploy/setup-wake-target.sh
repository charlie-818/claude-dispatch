#!/bin/bash
# Make THIS Mac wakeable from another machine on the same LAN, and make Dispatch
# come back by itself once it wakes.
#
#   bash deploy/setup-wake-target.sh
#
# Run it ONCE, sitting at the machine you want to be able to wake. It cannot be
# run remotely, which is the whole point: a Mac that is not already set up this
# way is unreachable while it sleeps, so there is no remote path in.
#
# Wake-on-LAN fails silently by default on a laptop, for four separate reasons.
# This script fixes the three that are scriptable and checks the fourth:
#
#   1. `womp` off          — the NIC is not armed for magic packets at all.
#   2. deep hibernate      — RAM is powered down, so nothing is listening.
#   3. no Dispatch on wake — the machine wakes but the Yard never comes back.
#   4. rotating Wi-Fi MAC  — "Private Wi-Fi Address" changes the address the
#                            waker aims at. NOT scriptable; we detect and report.
#
# A magic packet is not routable, so the waking machine must be on this same LAN.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

say "1. Power settings (needs sudo)"
# -a = all power sources. Note macOS ignores wake-on-network on battery regardless,
# so this machine still needs to be on AC for a wake to land.
sudo pmset -a womp 1 hibernatemode 0 standby 0 tcpkeepalive 1 powernap 1
ok "womp=1 (wake on magic packet), hibernatemode=0, standby=0, tcpkeepalive=1"
warn "macOS ignores wake-on-network on BATTERY — leave this Mac on AC to be wakeable"

say "2. Wi-Fi MAC stability"
WIFI_DEV="$(networksetup -listallhardwareports \
  | awk '/Hardware Port: Wi-Fi/{getline; print $2; exit}')"
if [[ -z "${WIFI_DEV:-}" ]]; then
  warn "no Wi-Fi interface found — if this Mac is on Ethernet, WoL is more reliable anyway"
else
  MAC="$(ifconfig "$WIFI_DEV" | awk '/ether/{print $2; exit}')"
  FIRST_OCTET=$(( 16#$(echo "$MAC" | cut -d: -f1) ))
  echo "  $WIFI_DEV MAC: $MAC"
  if (( FIRST_OCTET & 2 )); then
    bad "this is a ROTATING Private Wi-Fi Address — it will change, and the waker"
    echo "     will keep aiming at an address that no longer exists."
    echo "     Fix (not scriptable, Apple exposes no CLI for it):"
    echo "       System Settings → Wi-Fi → your network → Details… →"
    echo "       Private Wi-Fi Address → Off, then rejoin the network."
    echo "     Re-run this script afterwards to confirm."
  else
    ok "burned-in MAC — stable, safe to cache"
  fi
fi

say "3. Dispatch restarts itself after a wake"
bash "$REPO_DIR/deploy/install-launchd.sh" ensure
ok "com.ccdispatch.ensure installed — re-checks port 8788 every 120s"
echo "     (it opens iTerm2 and starts the server inside it, because the server"
echo "      needs ITERM2_COOKIE and hangs forever if launched headless)"

say "4. Teaching the waker this machine's address"
echo "  Nothing to do here. While this Mac is awake and on the tailnet, the other"
echo "  machine's Dispatch learns its LAN IP + MAC from ARP on its own and caches"
echo "  them, so the Wake button appears the next time this one is asleep."
echo
echo "  To seed it by hand instead, run THIS on the waking machine:"
echo "      python3 -c \"import wake; print(wake.remember('$(hostname -s | tr 'A-Z' 'a-z').<tailnet>.ts.net'))\""

say "Done."
echo "Verify from the other Mac, with this one asleep:"
echo "    python3 wake.py status <this-host>.<tailnet>.ts.net"
echo "    python3 wake.py wake   <this-host>.<tailnet>.ts.net --wait 90"
