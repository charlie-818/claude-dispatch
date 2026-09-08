# Architecture

CC Dispatch is a single aiohttp process that attaches to an already-running iTerm2, polls the panes it recognizes as agent CLIs, and exposes what it sees over HTTP and WebSocket to a passkey-gated PWA. It never spawns a session on its own initiative (the one exception is `/api/spawn`, an explicit user action) and never restarts anything — killing the server leaves the fleet untouched.

## Components

```
  iTerm2 panes ──► iTerm2 Python API ──► server.py ──► PWA (static/index.html)
  (Claude/Codex/Grok)                       │              ▲
                                            │              │  passkey / WebAuthn
   ~/.claude/statusline.sh ─► CC_FLEET_DIR  │              │  web-push
   ~/.claude/cc-active.sh   ─►              │              │
        (fleet status .json) ───────────────┘         phone, over Tailscale
                                            │
                          auth.py (sessions, WebAuthn) ── .credentials.json / .sessions.json
                          vault.py (credential broker) ── .integrations.vault / .grants.json
```

- **`server.py`** — the whole app: iTerm2 polling, fleet assembly, route handlers, WebSocket streams, hot reload.
- **`auth.py`** — bootstrap token exchange, WebAuthn registration/login, session cookies, CSRF guard, audit log.
- **`vault.py`** — encrypted credential store + request/grant bookkeeping for the agent auth broker.
- **`static/`** — the PWA (service worker, web-push subscription, UI).

## Pane identity: providers

`server.py:98` `pane_provider(uuid, job, text)` decides which agent CLI (if any) owns a pane, checked in this order:

1. **`KNOWN_AGENTS`** (`server.py:59`) — a uuid→provider cache. Identity never expires once learned, because a pane sitting at a permission prompt stops repainting its statusline and its fleet file ages out — precisely when you need to still reach it.
2. **`job_provider`** (`server.py:87`) — the pane's foreground job name matched against `claude`/`codex`/`grok` (handling versioned binaries like `grok-1.0.5-macos`).
3. **The fleet record's own `provider` field**, or **`marker_provider`** (`server.py:78`) — TUI chrome unique to each client (e.g. Claude's "shift+tab to cycle", Codex's "Ask Codex to do anything"), matched only on strings that client itself draws, never a phrase that could merely be discussed on screen.
4. **`CHILD_JOBS`** fallback (`server.py:52`) — while an agent runs a Bash tool, its foreground job is the child (`node`, `caffeinate`), so an unresolved child job still defaults to `claude` rather than being dropped.

`is_agent_pane` (`server.py:119`) is just `pane_provider(...) is not None` — the single predicate every write route checks before typing into a pane.

## The poll loop

There is no single "poll_loop" function; polling happens per WebSocket connection and inside `build_fleet` (`server.py:1392`), called once per second by `ws_fleet` and on-demand by `GET /api/fleet`.

- **Pane text**: `pane_text` (`server.py:474`) calls `session.async_get_screen_contents()` and joins the visible rows, translating iTerm's NUL-filled unwritten cells back to spaces.
- **Diffing**: `ws_pane` (`server.py:2966`) polls a single pane every `POLL` seconds (0.45s) and only sends a WebSocket frame when `pane_text` differs from the last value sent — the client never re-renders on a no-op poll.
- **Fleet assembly**: `build_fleet` (`server.py:1392`) merges, per live iTerm2 session: the pane's own screen text (for provider detection, mode, prompt), the fleet-status JSON dropped by the hooks (`read_fleet_files`, below), git churn (`git_snapshot`/`churn_for`), and the transcript-derived file/prompt counters. Rows are sorted so a pane blocked on a prompt always sorts first.

## Fleet status files

Claude Code's `statusLine` hook and Codex/Grok's `cc-active.sh` hook (see `examples/`) write one JSON file per session into `CC_FLEET_DIR` (default `/tmp/cc-status`), tagged with the pane's iTerm2 session id (`iterm_pane`). `read_fleet_files` (`server.py:993`):

- Reads every `*.json` in `CC_FLEET_DIR`, and a matching `<key>.state` file for `working`/`idle`/`ended`.
- Applies different staleness windows per provider: Claude's statusline repaints on every render, so 90s of silence (`STALE`) means the pane is gone; Codex/Grok only write at lifecycle events, so they get a much longer window (`STALE_GENERIC` = 4h, or `STALE_ENDED` = 15m once the state file says `ended`).
- Resolves the file into a `uuid` (parsed out of `iterm_pane`) or, if that env var was lost by a hook child process, indexes it by `tty` in `_FLEET_BY_TTY` so `build_fleet` can still match it to the live pane it's already looking at.
- When two records claim the same pane (e.g. a `/clear` or `--resume` leaves the old chat's file behind, still within its stale window), the most recently modified one wins.

## Transcript discovery per provider

- **Claude**: each fleet record carries `transcript_path` directly (Claude names its own `.jsonl` under `~/.claude/projects/<proj>/<session>.jsonl`). `session_ops` (`server.py:747`) incrementally scans it for prompt/file/tool-call counters.
- **Codex**: `journal_path("codex", sid)` (`server.py:826`) globs `~/.codex/sessions/*/*/*/*-<sid>.jsonl` (a Codex rollout journal) and caches the resolved path per `(provider, sid)`. `codex_ops` (`server.py:849`) incrementally parses `apply_patch` payloads inside `custom_tool_call` entries for file/line-delta counts.
- **Grok**: `journal_path("grok", sid)` globs `~/.grok/sessions/*/<sid>/chat_history.jsonl`. `grok_ops` (`server.py:922`) reads that same shape of journal.
- Both Codex and Grok fall back to `journal_path` only when the fleet record itself carries no `transcript_path` — Claude always does.
- Claude's own top-level session history (used by `GET /api/history`) is read separately from `_PROJECTS_DIR = ~/.claude/projects` (`server.py:2572`), scanning only top-level `<session>.jsonl` files (subagent/workflow files live in subdirectories and are skipped).

## WebSocket streams

- **`GET /ws/pane/{uuid}`** (`server.py:2966`) — per-pane live view. Sends `{history, cols}` once (scrollback, expensive to fetch, rarely changes), then `{text, cols, prompt, suggest}` on every screen change, `{gone: true}` once and closes if the pane disappears.
- **`GET /ws/fleet`** (`server.py:3002`) — the whole fleet, once a second, only when the serialized payload actually changed. Sends `{sessions, limits, pending}` (`pending` = `vault.pending_count()`, the outstanding credential requests badge).

## The write gate

Every route that sends keystrokes (`/api/key`, `/api/select`, `/api/send`, `/api/submit`, `/api/mode`, `/api/effort`, `/api/cmd`, `/api/model`) re-derives the pane's job name and screen text fresh, then calls `is_agent_pane(uuid, job, text)` (`server.py:119`) immediately before writing. If that returns `False` the handler refuses with a 403 naming the offending job — a scratch shell or unrelated pane is unreachable by construction, not by a stale cached flag. `claude_only` (`server.py:128`) is a second, narrower gate on top of that for the three Claude-only controls (mode/effort/model), since Codex and Grok draw none of that TUI chrome.

This is distinct from the `@guard`/`@writes` decorators (`server.py:1661`, `:1675`), which gate *who is allowed to call the route at all* (session + CSRF), not *which pane may receive keys* — see [SECURITY-MODEL.md](SECURITY-MODEL.md) and [DEVELOPMENT.md](DEVELOPMENT.md).

## Hot reload

Deploys don't relaunch the process externally — the server holds an iTerm2 API connection authorized by the `ITERM2_COOKIE` it was launched with, and only a process that *inherits* that cookie can reconnect without a fresh GUI trust prompt. `deploy-pull.sh` sends `kill -HUP $(cat .server.pid)` after a Python file changes; `_install_hot_reload` (`server.py:3437`) catches `SIGHUP`, drains the HTTP runner (bounded to 5s so a stuck WebSocket can't wedge the reload), then calls `os.execv(sys.executable, [...])` (`server.py:3457`) to re-exec the **same process** — so the cookie and every other inherited env var carry over, the listening socket (close-on-exec) frees up, and the fresh image rebinds the port cleanly. Frontend-only changes need no reload; `static/index.html` is read from disk per request.

See also: [API.md](API.md) for the full route table, [CONFIGURATION.md](CONFIGURATION.md) for env vars and on-disk state, [DEPLOYMENT.md](DEPLOYMENT.md) for how a deploy actually triggers this.
