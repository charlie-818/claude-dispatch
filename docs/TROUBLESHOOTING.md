# Troubleshooting

## Hangs at startup, no QR ever prints

**Cause**: the server was launched headless (ssh, a bare shell, or directly under `launchd`'s `com.ccdispatch` job) — it needs `ITERM2_COOKIE`, which only exists on a process iTerm2 itself spawned, to connect to the Python API without a GUI trust prompt.
**Fix**: launch it from inside a real iTerm2 window (`./start-dispatch.sh`), or install the `com.ccdispatch.ensure` watchdog instead of `com.ccdispatch` — see [DEPLOYMENT.md](DEPLOYMENT.md).

## Startup hangs even inside iTerm2

**Cause**: the iTerm2 Python API is disabled.
**Fix**: iTerm2 → Settings → General → Magic → enable "Python API".

## Passkey registration refuses / errors "passkeys need https on the tailnet name"

**Cause**: `auth.passkey_capable` refuses a bare IP or non-HTTPS origin (`rp_id` must be a real hostname). You're likely on `http://127.0.0.1:8788` directly instead of the `tailscale serve` HTTPS origin.
**Fix**: browse to the `https://<host>.<tailnet>.ts.net` URL the server prints at startup, not the raw bind address. Confirm `tailscale serve --bg 8788` is actually running.

## Pane doesn't show up in the fleet

**Cause 1**: the fleet-status hooks aren't installed for that pane's Claude Code (`~/.claude/settings.json` missing `statusLine`/hooks — see [CONFIGURATION.md](CONFIGURATION.md)).
**Cause 2**: `CC_FLEET_DIR` doesn't match between the hook and the server (env var set differently for the two processes).
**Fix**: run `make hooks` (or copy `examples/statusline.sh` + `examples/cc-state.sh` into `~/.claude/`, merge `examples/settings.snippet.json`), and confirm `CC_FLEET_DIR` (default `/tmp/cc-status`) is consistent. Note a pane with no hook can still appear if iTerm2 reports the agent binary as its foreground job — the hooks only add richer metrics (cost, tokens, counters).

## Keystrokes/commands are rejected with "shows no agent UI — refusing"

**Cause**: the write gate (`is_agent_pane`) re-checked the pane's live job name and screen text and found neither an agent binary nor recognizable TUI chrome — most likely the pane is mid-startup, at a plain shell prompt, or running something else entirely.
**Fix**: this is by design (see [SECURITY-MODEL.md](SECURITY-MODEL.md)) — wait for the agent's TUI to actually draw, or confirm you're targeting the right pane uuid.

## Mode/effort/model buttons say "this pane is running codex/grok"

**Cause**: `claude_only` (`server.py`) refuses those three controls on any pane not identified as `claude` — Codex and Grok draw no equivalent status-line chrome for the watch-and-press loop to key off.
**Fix**: expected behavior, not a bug — those controls are Claude-only.

## Push notifications never arrive

**Cause 1**: no VAPID keypair generated (`GET /api/vapid` returns `enabled: false`).
**Cause 2**: the browser never completed `POST /api/push/subscribe`.
**Fix**: check `GET /api/vapid` — `enabled` should be `true` and `subs` > 0. If `enabled` is false, `vapid_private.pem`/`vapid_public.txt` are missing; generate a VAPID keypair and place them at the repo root. Re-enable notifications in the PWA to re-subscribe if `subs` is 0.

## `/api/whisper` returns 500 or 503

**503** — the model file isn't present at `WHISPER_MODEL` (default `models/ggml-base.en.bin`); download it per [CONFIGURATION.md](CONFIGURATION.md).
**500 "transcription failed"** — `whisper-cli` or `ffmpeg` exited non-zero; the response's `detail` carries the last 200 chars of stderr. Confirm both binaries are actually installed and `WHISPER_BIN`/`FFMPEG_BIN` resolve (a `launchd`/`nohup` launch has a bare `PATH` missing `/opt/homebrew/bin`, which `_find_bin` falls back to — check that path exists if `which whisper-cli` fails in your login shell but not in the launchd context).

## Deploy log says "server not listening" or "no live server pid"

**Cause**: `deploy-pull.sh` sent `SIGHUP` to the pid in `.server.pid`, but either the process wasn't actually alive (stale pidfile after a crash) or the reload didn't come back up within its 4s check.
**Fix**: `.server.pid` is only trustworthy while a server is actually running; if it's stale, start the server manually from inside iTerm2 (auto-deploy cannot itself launch a fresh process — see [DEPLOYMENT.md](DEPLOYMENT.md)). If the reload genuinely failed, check the terminal the server is running in (or `server.out` under launchd) for the traceback the new code raised on import.

## macOS Automation consent dialog blocks the watchdog

**Cause**: the first time `ensure-dispatch.sh`'s `osascript` calls try to drive iTerm2, macOS prompts "Terminal wants to control iTerm2" and blocks until answered.
**Fix**: approve it once in System Settings → Privacy & Security → Automation. `ensure-dispatch.sh`'s bounded `osa()` wrapper keeps a stuck prompt from wedging the launchd job forever — it just times out and retries on the next interval — but it can't resolve the dialog for you.

## Port already in use / "iTerm2 reconnect re-entered main()"

**Not actually an error** — iTerm2 re-invokes the Python API's `main()` whenever its API socket reconnects (e.g. after iTerm2 restarts). `server.py`'s `main()` catches `EADDRINUSE` when `_SERVING` is already `True` and logs that it's keeping the original live server rather than crashing. If you see a *different* process genuinely holding 8788 (e.g. a leftover server from before a manual restart), find it with `lsof -nP -iTCP:8788 -sTCP:LISTEN` and kill it before starting a new one.

## Stale `.server.pid` after a crash

**Cause**: the server died without cleaning up its own pidfile.
**Fix**: harmless on its own — `ensure-dispatch.sh` already checks the pid actually belongs to a `server.py` process (matched by command line, not just existence) before treating it as live, and `deploy-pull.sh`'s `kill -HUP` on a dead pid just fails silently and logs the "no live server pid" warning above.

## Bootstrap token rejected / "locked out, retry in Ns"

**Cause**: 5 wrong token guesses within 10 minutes trip a 15-minute IP lockout (`auth.FAIL_MAX`/`FAIL_WINDOW`/`LOCKOUT`).
**Fix**: wait out the lockout, or re-copy the token/URL exactly as printed at server startup (it's also written into the QR — scanning it is less error-prone than retyping).

## Scanning the QR opens a session that "doesn't exist" in my real browser

**Cause**: the QR scanner app opened the link in its own in-app browser, which received the session cookie for itself; switching to Chrome/Safari afterward finds no session there.
**Fix**: use the "paste the token" form in the fallback locked page (posts to `/auth/bootstrap` from whichever browser you're actually holding), or open the QR link directly in your real browser instead of the scanner's preview.

## The Wake button doesn't appear on a sleeping Mac

**Cause**: no MAC has ever been cached for it. Dispatch learns a peer's LAN IP and
MAC from ARP *while that peer is awake* — a machine that has been asleep since
before this feature shipped was never seen.
**Fix**: bring it online once (any means — a keypress will do). The next
`/api/devices` poll caches it, and the Wake button is there from then on. To seed
it by hand: `python3 wake.py learn <host>.<tailnet>.ts.net <mac>`.

## Wake button sends, but the Mac never comes back

Run `python3 wake.py status <host>` on the waking machine first — it reports what
is cached and warns about a rotating MAC. Then check, on the *target*:

**Cause 1**: Wake-on-LAN was never armed. `pmset -g | grep womp` must be `1`.
**Cause 2**: the MAC rotates. macOS "Private Wi-Fi Address" hands out a
locally-administered MAC that changes, so the cached one goes stale and the
packets are aimed at an address that no longer exists. `wake.py status` flags this
as a warning. Turn it off: System Settings → Wi-Fi → network → Details… → Private
Wi-Fi Address → Off.
**Cause 3**: deep hibernate — RAM is unpowered and nothing is listening. Needs
`hibernatemode 0` and `standby 0`.
**Cause 4**: it's on battery. macOS ignores wake-on-network unless on AC.
**Cause 5**: it isn't on the same LAN. A magic packet is not routable, so this
only ever works between machines on one network segment.

`bash deploy/setup-wake-target.sh`, run once physically on the target, fixes 1, 3
and reports 2. There is no remote fix — a Mac that isn't already set up this way
cannot be reached while it sleeps.
