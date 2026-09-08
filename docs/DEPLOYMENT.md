# Deployment

## Running by hand

The server **must** be launched from inside a real iTerm2 session, because it needs the `ITERM2_COOKIE` environment variable iTerm2 sets on processes it spawns — that cookie is what lets the iTerm2 Python API authorize the connection without a GUI trust prompt. Launched headless (ssh, a bare `launchd` job, `nohup`), the connect just hangs forever.

```bash
tailscale serve --bg 8788      # one terminal: TLS + tailnet reach
.venv/bin/python server.py     # another terminal, inside iTerm2
```

or use the wrapper, which does the same thing: `./start-dispatch.sh` (`cd` to the repo, `exec ./.venv/bin/python server.py`).

## Running under launchd

Three jobs, templated in `deploy/launchd/*.plist` (`__REPO__` substituted for the checkout path) and installed with `deploy/install-launchd.sh [ensure|server|deploy|all]` (default `ensure`):

- **`com.ccdispatch.ensure`** (recommended) — a watchdog (`ensure-dispatch.sh`), `RunAtLoad` + `StartInterval: 120`. It never runs the server itself: if nothing is listening on 8788, it opens iTerm2 (if not already running), waits for iTerm2's AppleScript API to answer, kills a stale server matched by `.server.pid`, then drives iTerm2 via `osascript` to open a new window and run `cd ~/claude-dispatch && ./start-dispatch.sh` inside it — so the server always starts with a real cookie. Idempotent and safe to run on a short interval. Logs to `ensure.out`.
- **`com.ccdispatch`** — runs `dispatch-launchd.sh` directly under launchd with `KeepAlive: true`. This only works if iTerm2 has granted API access to launchd-spawned processes, which is unusual — most setups should use `ensure` instead. Logs to `server.out`.
- **`com.ccdispatch.deploy`** — runs `deploy-pull.sh` every 60 seconds via `StartInterval`, for a deploy target that should always mirror `origin/main`. Logs to `deploy.out`.

The first time `ensure-dispatch.sh`'s `osascript` calls run, macOS may show an Automation consent dialog ("Terminal wants to control iTerm2"); until that's approved, `osascript` blocks and the watchdog's bounded `osa()` wrapper (a background timeout) turns that into a logged failure that just retries on the next interval rather than wedging the launchd job forever.

## Auto-deploy target setup

`deploy-pull.sh` runs **on the deploy target only**. Semantics:

1. `git fetch origin main` then `git reset --hard origin/main` — this makes the checkout a **pure mirror** of `origin/main`. **Never edit code directly on the target** — a deploy wipes it. Runtime state (`.sessions.json`, `.integrations.vault`, `.token`, etc.) is git-ignored, so `reset --hard` never touches it.
2. If `HEAD` moved and any changed path ends in `.py`, it sends `SIGHUP` to the PID in `.server.pid` — the live server drains its runner and `os.execv`s itself in place (see [ARCHITECTURE.md](ARCHITECTURE.md#hot-reload)), inheriting the iTerm2 cookie so it reconnects silently. It then waits 4s and checks `lsof -iTCP:8788` to confirm the reload actually came back up, logging a `WARN` if not.
3. If the changed paths are frontend-only (`static/*`, no `.py`), nothing is restarted — `index.html` and static assets are served fresh off disk on every request.
4. If `.server.pid` is stale/missing (no live process), it logs a `WARN` that the code is on disk but needs a manual start from inside iTerm2 — auto-deploy cannot itself launch the server (see "Running by hand" above for why).

## Tailscale serve

`tailscale serve --bg 8788` terminates TLS and proxies to the loopback port — this is the only thing that makes the server reachable off the Mac, and it's also required for passkeys (WebAuthn needs a real HTTPS origin; `auth.passkey_capable` refuses a bare IP). `ts_serve_origin()` (`server.py:3339`) reads `tailscale serve status --json` to print the actual public origin (including a non-443 port, if the tailnet root is taken by something else) at startup.

## Upgrading

On the dev machine: commit and push to `main` as normal. On a deploy target running `com.ccdispatch.deploy`, the next poll (within 60s) pulls and hot-reloads automatically. On a machine you run by hand, `git pull` then either restart the server manually or send it `SIGHUP` yourself (`kill -HUP $(cat .server.pid)`) to reload in place without dropping the iTerm2 connection.

## Logs

| File | From |
|---|---|
| `server.out` | `com.ccdispatch` launchd job (stdout+stderr of the server when run directly under launchd). |
| `ensure.out` | `com.ccdispatch.ensure` watchdog. |
| `deploy.out` | `com.ccdispatch.deploy` / `deploy-pull.sh`. |
| `audit.log` | Every auth event and gated write (`auth.audit`) — see [SECURITY-MODEL.md](SECURITY-MODEL.md). |

When run by hand inside iTerm2 (not under launchd), server output goes to that terminal's scrollback instead of `server.out`.
