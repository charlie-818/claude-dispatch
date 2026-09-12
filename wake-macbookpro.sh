#!/bin/bash
# Wake macbookpro on demand and make CC Dispatch usable on it.
#
# macbookpro is a laptop that normally sleeps, and we want to keep it that way.
# A magic packet only buys a ~15s dark wake -- with the lid closed, nothing but
# `pmset disablesleep` holds the machine up. So this script wakes it, pins it
# awake for the session, and hands sleep back on --release.
#
#   ./wake-macbookpro.sh            wake, hold awake, verify dispatch
#   ./wake-macbookpro.sh --release  let it sleep normally again
#   ./wake-macbookpro.sh --status   where things stand
#   ./wake-macbookpro.sh --setup    one-time: install the scoped sudoers rule
#
# --setup is interactive (it asks for the macbookpro password); everything else
# runs unattended off the SSH key.
set -uo pipefail

# WAKE_HOST lets a caller (the /api/wake endpoint) name which target to wake;
# on its own the script is about macbookpro, which is the only one so far.
HOST_DNS=${WAKE_HOST:-macbookpro.tail320cc5.ts.net}
HOST_TS=$(/Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4 "${HOST_DNS%%.*}" 2>/dev/null || echo 100.87.27.76)
USER_AT=charliebc
TARGETS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.wake_targets.json"
LAN_BCAST=192.168.1.255
TS_BIN=/Applications/Tailscale.app/Contents/MacOS/Tailscale

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new)
say() { printf '%s\n' "$*"; }
progress() { printf '%s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

read_target() {
  python3 - "$TARGETS" <<'PY'
import json, os, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    sys.exit(f"cannot read wake targets: {e}")
want = os.environ.get("WAKE_HOST", "")
for host, t in d.items():
    if (host == want) if want else host.startswith("macbookpro"):
        print(t["ip"], t["mac"]); break
else:
    sys.exit(f"no wake-target entry for {want or 'macbookpro'}")
PY
}

# The stored MAC is a randomized private address; if the machine is in ARP we
# trust that over the file, since a rotation would leave the file stale.
current_mac() {
  local ip=$1 fromarp
  fromarp=$(arp -n "$ip" 2>/dev/null | grep -oE '([0-9a-f]{1,2}:){5}[0-9a-f]{1,2}' | head -1)
  [ -n "$fromarp" ] && python3 -c "
import sys
print(':'.join(p.zfill(2) for p in sys.argv[1].split(':')))" "$fromarp"
}

send_wol() {
  local mac=$1 ip=$2
  python3 - "$mac" "$LAN_BCAST" "$ip" <<'PY'
import socket, sys
mac, targets = sys.argv[1], sys.argv[2:]
raw = bytes.fromhex(mac.replace(":", ""))
pkt = b"\xff" * 6 + raw * 16
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
for host in targets:
    for port in (7, 9):
        try: s.sendto(pkt, (host, port))
        except OSError as e: print(f"  (send to {host}:{port} failed: {e})")
PY
}

ssh_to() { ssh "${SSH_OPTS[@]}" "$USER_AT@$1" "$2" 2>/dev/null; }

# sets $ip and $mac, preferring the live ARP entry over the stored MAC (the
# stored one is a randomized private address and will rotate eventually)
load_target() {
  local line live
  line=$(read_target) || exit 1
  read -r ip mac <<<"$line"
  [ -n "${ip:-}" ] && [ -n "${mac:-}" ] || die "wake target has no ip/mac"
  live=$(current_mac "$ip" || true)
  [ -n "$live" ] && mac=$live
  return 0
}

# Try the LAN address first (that is where the magic packet lands and where the
# dark wake answers soonest), then the tailnet address.
reach() {
  local ip=$1
  ssh_to "$ip" true && { echo "$ip"; return 0; }
  ssh_to "$HOST_TS" true && { echo "$HOST_TS"; return 0; }
  return 1
}

wake_and_connect() {
  local ip=$1 mac=$2 addr i
  if addr=$(reach "$ip"); then progress "already awake ($addr)"; echo "$addr"; return 0; fi
  progress "sending magic packet to $mac ..."
  send_wol "$mac" "$ip"
  for i in $(seq 1 25); do
    if addr=$(reach "$ip"); then progress "awake after ~${i} attempt(s) ($addr)"; echo "$addr"; return 0; fi
    ping -c 2 -W 1000 "$ip" >/dev/null 2>&1
  done
  return 1
}

dispatch_ok() { curl -sS --max-time 8 -o /dev/null -w '%{http_code}' "https://$HOST_DNS/api/ping" 2>/dev/null; }

cmd_setup() {
  local ip mac addr; load_target
  addr=$(wake_and_connect "$ip" "$mac" | tail -1) || die "could not reach macbookpro to set it up"
  say
  say "Installing a sudoers rule that allows pmset (and nothing else) without a"
  say "password. You will be asked for the macbookpro login password."
  say
  # Written to a temp file and validated with visudo before being installed --
  # a malformed sudoers file would lock sudo out entirely.
  ssh -t -o StrictHostKeyChecking=accept-new "$USER_AT@$addr" '
    set -e
    tmp=$(mktemp)
    printf "%s ALL=(root) NOPASSWD: /usr/bin/pmset\n" "$(id -un)" > "$tmp"
    sudo visudo -cf "$tmp" >/dev/null || { echo "refusing to install: sudoers syntax check failed"; rm -f "$tmp"; exit 1; }
    sudo install -m 440 -o root -g wheel "$tmp" /etc/sudoers.d/pmset-nopasswd
    rm -f "$tmp"
    echo "installed /etc/sudoers.d/pmset-nopasswd"
    sudo -n pmset -g >/dev/null 2>&1 && echo "verified: passwordless pmset works" || echo "WARNING: passwordless pmset still not working"
  ' || die "setup failed"
  say
  say "Done. From now on: ./wake-macbookpro.sh"
}

cmd_wake() {
  local ip mac addr code; load_target
  addr=$(wake_and_connect "$ip" "$mac" | tail -1) || die "macbookpro did not wake (is it powered off, or off this LAN?)"

  say "pinning it awake ..."
  local pinned=no
  if ssh_to "$addr" 'sudo -n pmset -a disablesleep 1 2>/dev/null'; then
    say "  sleep disabled"; pinned=yes
  else
    say "  COULD NOT pin it awake - run: $0 --setup"
  fi

  say "checking dispatch ..."
  if ssh_to "$addr" 'pgrep -f "claude-dispatch/server.py" >/dev/null'; then
    say "  server.py already running"
  else
    say "  not running - starting it via launchd"
    ssh_to "$addr" "launchctl kickstart gui/\$(id -u)/com.charliebc.ccdispatch 2>&1 | head -2"
  fi

  code=$(dispatch_ok)
  if [ "$code" = 200 ]; then
    say
    if [ "$pinned" = yes ]; then
      say "READY -> https://$HOST_DNS"
      say "(run '$0 --release' when you are done so it can sleep again)"
    else
      say "dispatch answers, but it is NOT pinned awake - it will sleep again in"
      say "~15s. Run '$0 --setup' once to fix that for good."
      return 1
    fi
  else
    say "  dispatch not answering yet (http ${code:-none}); give it a few seconds"
    return 1
  fi
}

cmd_release() {
  local ip mac addr; load_target
  if ! addr=$(reach "$ip"); then say "macbookpro is already asleep or unreachable - nothing to release"; return 0; fi
  ssh_to "$addr" 'sudo -n pmset -a disablesleep 0 2>/dev/null && echo "sleep re-enabled - it will sleep on its own" || echo "FAILED to re-enable sleep"'
  ssh_to "$addr" 'pkill -f "caffeinate -dimsu" 2>/dev/null; true'
}

cmd_status() {
  local ip mac addr code; load_target
  say "target     : $HOST_DNS"
  say "MAC        : $mac"
  if addr=$(reach "$ip"); then
    say "ssh        : up ($addr)"
    say "sleep      : $(ssh_to "$addr" 'pmset -g | grep -E "^[[:space:]]*(SleepDisabled|sleep)[[:space:]]" | head -2 | tr "\n" "; "')"
    say "power      : $(ssh_to "$addr" "pmset -g batt | tail -1 | sed 's/^[[:space:]]*//'")"
    say "dispatch   : $(ssh_to "$addr" 'pgrep -f "claude-dispatch/server.py" >/dev/null && echo running || echo "not running"')"
  else
    say "ssh        : unreachable (asleep)"
  fi
  code=$(dispatch_ok); say "https      : http ${code:-none}"
}

case "${1:---wake}" in
  --wake|"") cmd_wake ;;
  --release) cmd_release ;;
  --status)  cmd_status ;;
  --setup)   cmd_setup ;;
  -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown option: $1 (try --help)" ;;
esac
