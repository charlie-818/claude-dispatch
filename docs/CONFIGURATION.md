# Configuration

Environment variables, on-disk state, fleet hooks, and voice-input setup.

## Environment variables

| Var | Default | File:line | Purpose |
|---|---|---|---|
| `DISPATCH_PORT` | `8788` | `server.py:28` | Server port. |
| `DISPATCH_BIND` | `127.0.0.1` | `server.py:33` | Bind address. Loopback on purpose — see [SECURITY-MODEL.md](SECURITY-MODEL.md). |
| `CC_FLEET_DIR` | `/tmp/cc-status` | `server.py:34` | Where the statusline/cc-active hooks drop per-session fleet-status JSON. |
| `WHISPER_BIN` | resolved via `_find_bin("whisper-cli")` (PATH, then `/opt/homebrew/bin`, `/usr/local/bin`) | `server.py:1994` | whisper.cpp CLI binary. |
| `WHISPER_MODEL` | `models/ggml-base.en.bin` (relative to repo root) | `server.py:1996` | Local transcription model path. |
| `FFMPEG_BIN` | resolved via `_find_bin("ffmpeg")` | `server.py:1997` | Used to normalize recorded audio to 16k mono wav before whisper. |
| `VAPID_SUB` | `mailto:admin@example.com` | `server.py:2770` | Contact `mailto:` claimed in web-push VAPID JWTs. |
| `DISPATCH_GITHUB_CLIENT_ID` | `""` (unset) | `server.py:3046` | GitHub OAuth App client id for the device-flow integration; without it `/api/integrations/device/start` refuses and the UI falls back to pasting a token. |

## On-disk state files

All of these are per-install, git-ignored (see `.gitignore`), and mostly `0600`. None are needed to run from a clean checkout — they're created on first use.

| File | Written by | Holds |
|---|---|---|
| `.credentials.json` | `auth.py` (`save_creds`) | Registered WebAuthn passkeys: credential id, public key, sign count, rp_id, added date. No secrets — a public key can't be used to authenticate without the phone's private key. |
| `.sessions.json` | `auth.py` (`save_sessions`) | Session records keyed by session id: `level` (`bootstrap`/`verified`), `ip`, `created`, `last`. Survives a server restart so the phone isn't logged out. |
| `.grants.json` | `vault.py` (`save_grants`) | Pending credential `requests` and approved `grants` — pane uuid, service/cred id, scopes, timestamps. No secret values. |
| `.token` | not directly read by server.py at runtime beyond the in-memory `TOKEN` it mints at boot; `dispatch-auth`/ops scripts reference it as the bootstrap token file convention | The single-use bootstrap token baked into the startup QR. |
| `.integrations.vault` | `vault.py` (`save_vault`) | Fernet-encrypted blob of every stored credential (provider, label, secret, env_var, scopes, expiry, last4). Ciphertext only — see [SECURITY-MODEL.md](SECURITY-MODEL.md). |
| *(macOS Keychain item `cc-dispatch-vault`/`datakey`)* | `vault.py` (`vault_key`) | The Fernet data key that decrypts `.integrations.vault`. Fetched via the `security` CLI; falls back to a `.vault.key` file (shouted about as insecure) only when no Keychain is available. |
| `vapid_private.pem` / `vapid_public.txt` | generated once, read at `server.py:2765` | Web-push VAPID keypair for this install; private key signs push payloads, public key is handed to browsers to subscribe. |
| `.push_subs.json` | `server.py` (`_save_subs`) | Registered browser push subscriptions (endpoint + keys per device). |
| `.peers.json` | manually maintained, read by `load_peers` (`server.py:3359`) | Optional map of `{tailnet_host: base_url}` for Dispatch instances that don't serve at their tailnet root (e.g. a host publishing on `:8443` because the root is taken by something else). |
| `.summaries.json` | `server.py` (`_save_summaries`) | Cached `claude -p` session summaries, keyed by pane uuid + prompt count, so repeat "eye" taps in the UI don't re-pay the LLM call. May contain transcript-derived text. |
| `.churn.json` | `server.py` (`_save_churn`) | Per-session git churn baseline (lines added/removed/files touched) so a restart doesn't re-count history as new changes. |
| `audit.log` | `auth.py` (`audit`) | Append-only JSON-lines log of every auth event and gated write: timestamp, action, ip, session level, detail. |
| `.server.pid` | `server.py` (`_write_pidfile`) | PID of the live server process, used by `deploy-pull.sh` to target the `SIGHUP` hot-reload signal. |

## Fleet hooks

Panes only show up with full metrics (files/prompts/model/cost) when they publish status. Ready-to-install hooks live in `examples/`:

- **`examples/statusline.sh`** — Claude Code's `statusLine` command. Reads the JSON blob Claude pipes to stdin on every repaint, tags it with the pane's iTerm2 session id (`iterm_pane`), and writes it atomically into `CC_FLEET_DIR/<session_id>.json`.
- **`examples/cc-state.sh`** — hooked to `UserPromptSubmit` (writes `working`) and `Stop`/`Notification` (writes `idle`) so `read_fleet_files` can report a pane's working/idle state even between statusline repaints.
- **`examples/settings.snippet.json`** — the exact keys to merge into `~/.claude/settings.json` to wire both up (`statusLine` + the three hooks). `make hooks` copies both scripts into `~/.claude/` and chmods them.

Panes with no status file are still driveable when iTerm2 reports an agent binary as the foreground job (`job_provider`) — the hooks just make the fleet view complete (cost, tokens, model, transcript-derived counters).

## Whisper setup

`/api/whisper` transcribes phone-recorded audio entirely locally:

```bash
brew install whisper-cpp
mkdir -p models
curl -L -o models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

Override `WHISPER_BIN`/`WHISPER_MODEL`/`FFMPEG_BIN` if the binaries or model live elsewhere. If the model file is missing, the route returns `503` rather than failing silently.
