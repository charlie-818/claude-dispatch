# CC Dispatch

**Phone control for a fleet of live Claude Code panes.**

CC Dispatch reads and drives your *existing* iTerm2 Claude Code sessions from
your phone. See every pane's status at a glance, read what each one is doing,
answer permission prompts, send follow-ups, and get a push notification the
moment a session finishes or needs input — all from a passkey-protected PWA
served over your Tailnet.

Nothing is restarted. The server attaches to sessions that are already running
(via the iTerm2 Python API) and only ever writes in response to an
authenticated request from the UI. Kill the server and your fleet is exactly as
it was.

---

## How it works

```
  iTerm2 panes ──► iTerm2 Python API ──► server.py ──► PWA (static/index.html)
   (Claude CC)                              │              ▲
                                            │              │  passkey / WebAuthn
   ~/.claude/statusline.sh ─► /tmp/cc-status│              │  web-push
        (fleet status .json) ───────────────┘         your phone, over Tailscale
```

- **`server.py`** — aiohttp server. Polls iTerm2 for pane contents, reads
  Claude session transcripts, exposes the fleet over HTTP/WebSocket, and sends
  keystrokes back to panes that are confirmed to be running Claude.
- **`auth.py`** — WebAuthn/passkey login. The bootstrap token in the QR is
  single-use to register the first passkey; after that, only passkeys get in.
- **`vault.py` + `dispatch-auth`** — the credential broker. An agent in a pane
  runs `dispatch-auth request <service>`; you approve it on your phone; the
  secret is released just-in-time and never touches disk in plaintext.
- **`static/`** — the installable PWA (service worker + web-push).

### Security model

- Binds to **loopback (`127.0.0.1`) by default**. Reachability is Tailscale's
  job: `tailscale serve` terminates TLS and proxies to the local port, so there
  is no listener on any network interface for a stranger to find.
- **Passkey-gated.** Registration requires the one-time bootstrap token *and* a
  real TLS origin (the Tailnet host), so passkeys can't be registered over a
  bare IP.
- **Writes are gated.** A pane only receives keystrokes if its foreground job is
  Claude (or it recently wrote a fleet-status file). Scratch shells and
  unrelated panes are unreachable by construction.
- Integration secrets live in an encrypted vault (`.integrations.vault`); the
  Fernet key is stored in the macOS login keychain via the `security` CLI.

All per-install secrets and state are **git-ignored** — see `.gitignore`. This
repo contains code only.

---

## Requirements

- **macOS** with **iTerm2** (the Python API must be enabled:
  *iTerm2 → Settings → General → Magic → Enable Python API*).
- **Python 3.11+**.
- **[Tailscale](https://tailscale.com/)** for TLS + remote reach (passkeys
  require a real HTTPS origin).
- *(Optional, voice input)* **[whisper.cpp](https://github.com/ggerganov/whisper.cpp)**
  (`brew install whisper-cpp`) + a local model — see below.
- CC Dispatch surfaces panes that publish status to `CC_FLEET_DIR`
  (default `/tmp/cc-status`). Ready-to-use hooks that do this ship in
  [`examples/`](examples/) — see *Wire up the fleet-status hooks* below. Panes
  without a status file are still driveable when iTerm2 reports Claude as the
  foreground job; the hooks just make the fleet view complete.

## Install

```bash
git clone https://github.com/<your-username>/claude-dispatch.git
cd claude-dispatch

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Wire up the fleet-status hooks

So every Claude Code pane shows up in the fleet, install the two helper scripts
and point Claude Code at them:

```bash
cp examples/statusline.sh examples/cc-state.sh ~/.claude/
chmod +x ~/.claude/statusline.sh ~/.claude/cc-state.sh
```

Then merge the keys from [`examples/settings.snippet.json`](examples/settings.snippet.json)
into `~/.claude/settings.json`. They:

- set `statusLine` to `statusline.sh`, which writes each pane's status JSON
  (tagged with its iTerm2 session id) into `CC_FLEET_DIR`, and
- add `UserPromptSubmit` / `Stop` / `Notification` hooks that call
  `cc-state.sh` to record whether the pane is `working` or `idle`.

New panes appear in the app within a couple of seconds of their first repaint.

### Run

```bash
# In one terminal: expose the port over your Tailnet with TLS
tailscale serve --bg 8788

# In another: start the server (attaches to your live iTerm2 fleet)
.venv/bin/python server.py
```

On startup the server prints a **QR code** and a URL with the bootstrap token
baked in. Scan it with your phone, register a passkey, and add the PWA to your
home screen. That's it — subsequent visits are passkey-only.

### Voice input (optional)

The `/api/whisper` endpoint transcribes recorded audio locally — nothing leaves
the machine. Install the CLI and fetch a model once:

```bash
brew install whisper-cpp
mkdir -p models
curl -L -o models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

Override the binary/model with `WHISPER_BIN` / `WHISPER_MODEL` if they live
elsewhere.

### Configuration

| Env var         | Default          | Purpose                                    |
|-----------------|------------------|--------------------------------------------|
| `DISPATCH_PORT` | `8788`           | Server port.                               |
| `DISPATCH_BIND` | `127.0.0.1`      | Bind address. Leave on loopback.           |
| `CC_FLEET_DIR`  | `/tmp/cc-status` | Where pane status `.json` files are read.  |
| `VAPID_SUB`     | `mailto:admin@example.com` | Contact `mailto:` for web-push.  |
| `WHISPER_BIN`   | `whisper-cli`    | whisper.cpp binary.                        |
| `WHISPER_MODEL` | `models/ggml-base.en.bin` | Local transcription model.        |

---

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE).

<!-- deploy pipeline test a5c8725 -->
<!-- deploy test2 e4b2d4f -->
