"""Wake a sleeping tailnet Mac with a Wake-on-LAN magic packet.

This is deliberately more than `socket.sendto(magic_packet)`, because three macOS
facts each make a naive waker fail *silently* — the packets send, nothing wakes,
and there is no error anywhere to tell you why:

1. **macOS rotates its Wi-Fi MAC.** "Private Wi-Fi Address" is on by default, per
   network, and hands out a locally-administered MAC that changes over time. A MAC
   pasted into a config file therefore goes stale with no warning. We never take a
   MAC from config: we LEARN it from ARP while the peer is awake and cache it.

2. **ARP forgets a sleeping host.** Once the peer stops answering, its ARP entry
   decays to "(incomplete)" — so the MAC is unavailable at exactly the moment you
   need it. The on-disk cache is what bridges that gap.

3. **Tailscale still knows where it lives.** `tailscale debug netmap` retains a
   peer's last-known `Endpoints` while it is offline, including the RFC1918 one.
   So the LAN IP survives sleep on its own and only the MAC needs caching.

A magic packet is not routable — it wakes a machine only on the same L2 segment.
This is therefore a LAN-local trick between two boxes on one network, not
something that works from the far side of the tailnet.

Waking also requires the *target* to be configured to listen (`pmset womp 1`, a
stable MAC, no deep hibernate). `preflight()` reports what is missing rather than
pretending a burst of unanswered packets was a success.
"""

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGETS_FILE = HERE / ".wake_targets.json"

# WoL is conventionally sent to discard(9) or echo(7); some NICs only arm one.
WOL_PORTS = (9, 7)


# ── pure helpers (unit-testable anywhere, no macOS or network needed) ─────────

def normalize_mac(raw):
    """'da:b9:f2:2f:3b:2' -> 'da:b9:f2:2f:3b:02', or None if it isn't a MAC.

    macOS `arp` prints octets WITHOUT leading zeros, so the naive split gives an
    11-character string and a magic packet built from it wakes nothing.
    """
    if not raw:
        return None
    raw = raw.strip().lower()
    if raw in ("(incomplete)", "incomplete"):
        return None
    parts = re.split(r"[:-]", raw)
    if len(parts) != 6:
        return None
    out = []
    for p in parts:
        if not p or len(p) > 2 or not all(c in "0123456789abcdef" for c in p):
            return None
        out.append(p.zfill(2))
    return ":".join(out)


def is_private(mac):
    """True if the locally-administered bit is set: a macOS Private Wi-Fi Address.

    This bit CANNOT distinguish "Fixed" from "Rotating" — macOS sets it for both.
    Fixed is perfectly wakeable (stable per network); Rotating is not. So the bit
    alone is never grounds for a warning: preflight() decides from observed
    stability instead, and only calls it rotating once it has seen it change.
    """
    mac = normalize_mac(mac)
    if not mac:
        return False
    return bool(int(mac.split(":")[0], 16) & 0x02)


# Back-compat alias: the bit means "locally administered", not "will rotate".
is_randomized = is_private


def magic_packet(mac):
    """6 x 0xFF followed by the target MAC repeated 16 times."""
    mac = normalize_mac(mac)
    if not mac:
        raise ValueError("bad MAC")
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def is_private_v4(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_private


# ── shelling out ─────────────────────────────────────────────────────────────

def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout
    except Exception:
        return ""


def ts_bin():
    """Same reasoning as server._ts_bin: GUI and ssh launches drop /usr/local/bin."""
    onpath = shutil.which("tailscale")
    if onpath:
        return onpath
    for p in ("/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
              "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if os.path.exists(p):
            return p
    return "tailscale"


def broadcast_addrs():
    """Every IPv4 broadcast address this host has, plus the global one.

    The subnet broadcast is what actually reaches the target; 255.255.255.255 is
    a belt-and-braces fallback for stacks that drop the directed form.
    """
    out = []
    for m in re.finditer(r"inet (\S+) netmask \S+ broadcast (\S+)", _run(["ifconfig"])):
        if is_private_v4(m.group(1)):
            out.append(m.group(2))
    out.append("255.255.255.255")
    seen, uniq = set(), []
    for a in out:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


def arp_mac(ip):
    """The MAC currently in the ARP cache for `ip`, or None if it has decayed."""
    m = re.search(r"at ([0-9a-fA-F:]+)", _run(["arp", "-n", ip]))
    return normalize_mac(m.group(1)) if m else None


def netmap_lan_ip(host):
    """The peer's last-known RFC1918 endpoint, which outlives its going offline."""
    raw = _run([ts_bin(), "debug", "netmap"], timeout=25)
    if not raw:
        return None
    try:
        peers = json.loads(raw).get("Peers") or []
    except Exception:
        return None
    want = host.rstrip(".").lower()
    for p in peers:
        name = (p.get("Name") or "").rstrip(".").lower()
        if name == want or name.split(".")[0] == want.split(".")[0]:
            for ep in p.get("Endpoints") or []:
                addr = ep.rsplit(":", 1)[0]
                if is_private_v4(addr):
                    return addr
    return None


# ── the learned-target cache ─────────────────────────────────────────────────

def load():
    try:
        return json.loads(TARGETS_FILE.read_text())
    except Exception:
        return {}


def save(data):
    tmp = TARGETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, TARGETS_FILE)
    try:
        os.chmod(TARGETS_FILE, 0o600)
    except OSError:
        pass


# Re-resolve a peer's LAN IP from the (slow) netmap at most this often; ARP alone
# is enough to keep a known address fresh in between.
STALE_AFTER = 3600

# Observe a private MAC unchanged for this long before trusting it as Fixed.
SETTLE_AFTER = 86400


def remember(host, ip=None, mac=None):
    """Record what we can see about `host` right now. Only ever *improves* an
    entry: a failed lookup never erases a good MAC learned while it was awake.

    This runs on every device poll, so it is deliberately frugal — it reuses the
    cached LAN IP instead of re-shelling to `tailscale debug netmap`, and writes
    the file only when a fact actually changed or the entry has gone stale.
    """
    host = host.rstrip(".")
    data = load()
    before = data.get(host) or {}
    entry = dict(before)
    stale = int(time.time()) - int(before.get("mac_seen") or 0) > STALE_AFTER

    ip = ip or (before.get("ip") if not stale else None) or netmap_lan_ip(host)
    if ip:
        entry["ip"] = ip
    mac = normalize_mac(mac) or (arp_mac(entry["ip"]) if entry.get("ip") else None)
    if mac:
        now = int(time.time())
        if before.get("mac") and before["mac"] != mac:
            # Caught it changing: proof of Rotating, not Fixed. This is the only
            # evidence that actually separates the two.
            entry["mac_changes"] = int(before.get("mac_changes") or 0) + 1
            entry["mac_first_seen"] = now
        elif not before.get("mac_first_seen"):
            entry["mac_first_seen"] = now
        entry["mac"] = mac
        entry["private"] = is_private(mac)
        entry["randomized"] = entry["private"]      # legacy key, same fact
    if not entry:
        return entry

    # Compare on facts about the peer, not on bookkeeping: mac_seen is written by
    # this branch, so including it would compare a value that cannot have changed
    # yet — which is how mac_first_seen/private silently failed to persist on the
    # first observation after an upgrade, leaving the Fixed-vs-Rotating evidence
    # permanently empty.
    def facts(d):
        return {k: v for k, v in d.items() if k != "mac_seen"}

    if facts(entry) != facts(before) or stale:
        if entry.get("mac"):
            entry["mac_seen"] = int(time.time())
        data[host] = entry
        save(data)
    return entry


def refresh(hosts):
    """Re-learn every host that is reachable now, so the cache is warm for later."""
    for h in hosts:
        try:
            remember(h)
        except Exception:
            continue


# ── sending ──────────────────────────────────────────────────────────────────

def send_magic(mac, ip=None, repeat=3):
    """Blast the magic packet every way that has a chance of landing.

    Returns the number of datagrams actually sent. Sending cannot confirm a wake —
    WoL is fire-and-forget with no acknowledgement — so the caller must poll.
    """
    pkt = magic_packet(mac)
    dests = [(b, p) for b in broadcast_addrs() for p in WOL_PORTS]
    if ip:
        # Unicast only lands while the ARP entry is still warm, but costs nothing.
        dests += [(ip, p) for p in WOL_PORTS]
    sent = 0
    for _ in range(repeat):
        for addr, port in dests:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(pkt, (addr, port))
                s.close()
                sent += 1
            except OSError:
                continue
    return sent


def is_up(ip, timeout=1.5):
    if not ip:
        return False
    return subprocess.run(["ping", "-c", "1", "-W", str(int(timeout * 1000)), ip],
                          capture_output=True).returncode == 0


# ── the public entry point ───────────────────────────────────────────────────

def preflight(host):
    """What do we know, and can we even try? Explains a 'no' instead of hiding it."""
    host = host.rstrip(".")
    entry = load().get(host) or {}
    mac, ip = entry.get("mac"), entry.get("ip") or netmap_lan_ip(host)
    if not mac:
        return {"ready": False, "host": host, "ip": ip,
                "reason": "no MAC learned yet — bring the machine online once so its "
                          "address can be cached from ARP, then it can be woken"}
    out = {"ready": True, "host": host, "ip": ip, "mac": mac}
    if not entry.get("private", entry.get("randomized")):
        return out                                   # burned-in address, nothing to say

    # A private address only matters if it ROTATES. Fixed is stable and wakes fine,
    # and the locally-administered bit cannot tell the two apart — so judge on what
    # we have actually observed rather than warning about every private address.
    changes = int(entry.get("mac_changes") or 0)
    # A cache entry written before we tracked first-seen has no evidence either way;
    # treat it as undecided rather than as "stable since 1970".
    first_seen = int(entry.get("mac_first_seen") or 0)
    stable_for = (int(time.time()) - first_seen) if first_seen else 0
    if changes:
        out["warn"] = (f"this address has changed {changes}x — the target is set to "
                       "Rotating; switch it to Fixed (or Off), Fixed wakes fine")
    elif stable_for < SETTLE_AFTER:
        out["warn"] = ("private Wi-Fi address, not yet observed long enough to tell "
                       "Fixed from Rotating — if it is set to Fixed, nothing to do")
    return out


def wake(host, wait=0):
    """Send the burst; optionally poll up to `wait` seconds for the host to answer.

    `woke` is only ever True on a real reply — an unanswered burst reports
    ok=True (we sent it) with woke=False, never a false success.
    """
    host = host.rstrip(".")
    pre = preflight(host)
    if not pre.get("ready"):
        return {"ok": False, **pre, "sent": 0, "woke": False}

    mac, ip = pre["mac"], pre.get("ip")
    sent = send_magic(mac, ip)
    result = {"ok": sent > 0, "host": host, "mac": mac, "ip": ip, "sent": sent,
              "woke": False, "waited": 0}
    if pre.get("warn"):
        result["warn"] = pre["warn"]
    if not sent:
        result["reason"] = "could not send any magic packet"
        return result

    deadline = time.time() + wait
    while wait and time.time() < deadline:
        if is_up(ip):
            result["woke"] = True
            break
        time.sleep(2)
    result["waited"] = int(min(wait, time.time() - (deadline - wait))) if wait else 0
    if wait and not result["woke"]:
        result["reason"] = ("magic packet sent but the host did not answer in "
                            f"{wait}s — it may have Wake-on-LAN disabled, be in deep "
                            "hibernate, or be off this LAN")
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────
# So the wake path is usable (and debuggable) from a terminal on the waking
# machine, without the Yard and without a passkey — useful precisely when the
# thing you are trying to fix is the Yard.
if __name__ == "__main__":
    import sys

    def _usage():
        print(__doc__.strip().splitlines()[0])
        print("\nusage:  python3 wake.py status <host>")
        print("        python3 wake.py learn  <host> [mac]")
        print("        python3 wake.py wake   <host> [--wait SECONDS]")
        raise SystemExit(2)

    argv = sys.argv[1:]
    if len(argv) < 2:
        _usage()
    cmd, host = argv[0], argv[1]
    rest = argv[2:]

    if cmd == "status":
        print(json.dumps(preflight(host), indent=2))
    elif cmd == "learn":
        mac = rest[0] if rest and not rest[0].startswith("-") else None
        print(json.dumps(remember(host, mac=mac), indent=2))
    elif cmd == "wake":
        secs = 0
        if "--wait" in rest:
            try:
                secs = int(rest[rest.index("--wait") + 1])
            except (IndexError, ValueError):
                _usage()
        result = wake(host, wait=secs)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result.get("ok") else 1)
    else:
        _usage()
