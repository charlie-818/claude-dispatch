# CC Dispatch

**Phone control for a fleet of live agent panes.**

[![CI](https://github.com/charlie-818/claude-dispatch/actions/workflows/ci.yml/badge.svg)](https://github.com/charlie-818/claude-dispatch/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![Website](https://img.shields.io/badge/website-charlie--818.github.io%2Fclaude--dispatch-informational)](https://charlie-818.github.io/claude-dispatch/)

CC Dispatch reads and drives your *existing* iTerm2 agent sessions — Claude Code,
Codex and Grok — from your phone, each with its own critter on the yard so you can
tell them apart. See every pane's status at a glance, read what each one is doing,
answer permission prompts, send follow-ups, and get a push notification the
moment a session finishes or needs input — all from a passkey-protected PWA
served over your Tailnet.

Nothing is restarted. The server attaches to sessions that are already running
(via the iTerm2 Python API) and only ever writes in response to an
authenticated request from the UI. Kill the server and your fleet is exactly as
it was.

[Website](https://charlie-818.github.io/claude-dispatch/) ·
[Docs](docs/README.md) ·
[Contributing](CONTRIBUTING.md) ·
[Security](SECURITY.md)

---

## Table of contents

- [How it works](#how-it-works)
- [Security model](#security-model)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Voice input](#voice-input-optional)
- [Configuration](#configuration)
- [Running in production](#running-in-production)
- [Documentation](#documentation)
- [Development](#development)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

---

## How it works

```
  iTerm2 panes ──► iTerm2 Python API ──► server.py ──► PWA (static/index.html)
  (Claude/Codex/Grok)                       │              ▲
                                            │              │  passkey / WebAuthn
   ~/.claude/statusline.sh ─► /tmp/cc-status│              │  web-push
   ~/.claude/cc-active.sh   ─►              │              │
        (fleet status .json) ───────────────┘         your phone, over Tailscale
```

- **`server.py`** — aiohttp server. Polls iTerm2 for pane contents, reads each
  session's transcript (Claude's `.jsonl`, Codex's rollout, Grok's chat log),
  exposes the fleet over HTTP/WebSocket, and sends keystrokes back to panes that
  are confirmed to be running one of those agents. Permission mode, effort and
  model are Claude-only controls and are hidden for the others.
- **`auth.py`** — WebAuthn/passkey login. The bootstrap token in the QR is
  single-use to register the first passkey; after that, only passkeys get in.
- **`vault.py` + `dispatch-auth`** — the credential broker. An agent in a pane
  runs `dispatch-auth request <service>`; you approve it on your phone; the
  secret is released just-in-time and never touches disk in plaintext.
- **`static/`** — the installable PWA (service worker + web-push).

## Security model

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
repo contains code only. Full writeup: [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md).

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

## Quick start

### Install

```bash
git clone https://github.com/charlie-818/claude-dispatch.git
cd claude-dispatch

# make
make venv

# raw
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`make venv` also installs `requirements-dev.txt` (pytest, ruff) so `make check`
works right away.

### Wire up the fleet-status hooks

So every Claude Code pane shows up in the fleet, install the two helper scripts
and point Claude Code at them:

```bash
# make
make hooks

# raw
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
# make
make run

# raw
.venv/bin/python server.py
```

On startup the server prints a **QR code** and a URL with the bootstrap token
baked in. Scan it with your phone, register a passkey, and add the PWA to your
home screen. That's it — subsequent visits are passkey-only.

## Voice input (optional)

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

## Configuration

| Env var                     | Default          | Purpose                                    |
|------------------------------|------------------|--------------------------------------------|
| `DISPATCH_PORT`              | `8788`           | Server port.                               |
| `DISPATCH_BIND`              | `127.0.0.1`      | Bind address. Leave on loopback.           |
| `CC_FLEET_DIR`               | `/tmp/cc-status` | Where pane status `.json` files are read.  |
| `VAPID_SUB`                  | `mailto:admin@example.com` | Contact `mailto:` for web-push.  |
| `WHISPER_BIN`                | `whisper-cli`    | whisper.cpp binary.                        |
| `WHISPER_MODEL`              | `models/ggml-base.en.bin` | Local transcription model.        |
| `FFMPEG_BIN`                 | `ffmpeg`         | ffmpeg binary, used for audio conversion.  |
| `DISPATCH_GITHUB_CLIENT_ID`  | *(empty)*        | GitHub OAuth device-flow client id for the integrations page; empty disables it. |

---

## Running in production

Details: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). In short:

- `deploy/install-launchd.sh ensure` installs the recommended reboot watchdog —
  the server should survive a reboot without you re-running anything by hand.
- The same script's `server` / `deploy` / `all` modes install the other launchd
  templates in `deploy/launchd/` (run-on-login, auto-deploy-on-push).
- Reachability is still `tailscale serve` in front of the loopback port — the
  watchdog doesn't change the security model above.

## Documentation

- [docs/README.md](docs/README.md) — index of everything below.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the pieces fit together.
- [docs/API.md](docs/API.md) — HTTP/WebSocket routes.
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — every env var and on-disk file.
- [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md) — threat model in full.
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — launchd, tailscale serve, the watchdog.
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — repo layout, running tests locally.
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — common failure modes.

## Development

```bash
make venv   # venv + requirements.txt + requirements-dev.txt
make check  # ruff + pytest
```

The `tests/` suite is 153 unit tests against the pure parsers and write gates
(pane detection, auth helpers, vault helpers, sweep auth) — no live iTerm2
needed, so it runs in CI on ubuntu and macOS. What it *can't* cover: anything
that requires a real iTerm2 session or a live agent pane — exercise those by
hand against a real fleet, as described in [CONTRIBUTING.md](CONTRIBUTING.md).

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Please don't open a public issue for a vulnerability — see
[SECURITY.md](SECURITY.md) for how to report one privately.

## License

[Apache License 2.0](LICENSE).
</content>
