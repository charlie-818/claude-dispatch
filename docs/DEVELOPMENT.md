# Development

## Dev setup

```bash
make venv      # python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
make check     # lint + test
```

Equivalent by hand: `.venv/bin/ruff check .` and `.venv/bin/python -m pytest`. `make run` starts the server (`.venv/bin/python server.py`) — remember it needs to be launched inside a real iTerm2 session, see [DEPLOYMENT.md](DEPLOYMENT.md).

## Repo layout

| Path | What it is |
|---|---|
| `server.py` | The whole app: iTerm2 polling, fleet assembly, all HTTP/WS routes, hot reload. |
| `auth.py` | Bootstrap token, WebAuthn registration/login, sessions, CSRF guard, audit log. |
| `vault.py` | Encrypted credential store + request/grant bookkeeping for the agent auth broker. |
| `dispatch-auth` | CLI an agent running inside a spawned pane uses to request/fetch/list credentials from the broker. |
| `icon.py` | Generates the PWA app icons. |
| `import_auths.py` / `export_auths.py` | Move a passphrase-encrypted bundle of passkeys/sessions/vault between two installs (e.g. two Macs). |
| `prune_vault.py` | Offline maintenance: drop stale/expired vault entries and grants. |
| `reprovider.py` | Offline maintenance: re-derive/backfill a fleet record's `provider` field. |
| `scan_deploy_env.py` | Scans a machine for already-configured service credentials to seed the vault from (reference-backed `add_source_cred`). |
| `sweep_auth.py` | Bulk sweep across known credential locations (npmrc, netrc, cloud CLI configs, …) feeding `vault.add_source_cred`. |
| `start-dispatch.sh` | `cd` + `exec .venv/bin/python server.py`; the by-hand / `dispatch-launchd.sh` entry point. |
| `dispatch-launchd.sh` | launchd wrapper: opens iTerm2, waits, execs the server in the foreground for `KeepAlive`. |
| `ensure-dispatch.sh` | Reboot/crash watchdog: drives iTerm2 via AppleScript to (re)start the server if nothing is listening. |
| `deploy-pull.sh` | Auto-deploy target script: `git reset --hard origin/main`, `SIGHUP` the live server on a Python change. |
| `requirements.txt` / `requirements-dev.txt` | Runtime deps / dev deps (adds pytest, pytest-asyncio, ruff). |
| `pyproject.toml` | Project metadata + ruff/pytest config. |
| `Makefile` | `venv`, `run`, `test`, `lint`, `fmt`, `check`, `serve-tailscale`, `hooks`, `site` targets. |
| `deploy/` | `install-launchd.sh` + `launchd/*.plist` templates for the three launchd jobs, and a `README.md` explaining them. |
| `examples/` | `statusline.sh`, `cc-state.sh`, `settings.snippet.json` — the fleet-status hooks, see [CONFIGURATION.md](CONFIGURATION.md). |
| `static/` | The PWA: `index.html`, service worker, icons. |
| `site/` | The public docs website (`charlie-818.github.io/claude-dispatch/`), served locally with `make site`. |
| `tests/` | Pytest suite — see below. |
| `docs/` | This directory. |
| `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`, `LICENSE` | Standard repo docs. |
| `.editorconfig`, `.gitignore`, `.github/` | Editor/CI/VCS config. |

## Running tests

```bash
.venv/bin/python -m pytest        # or: make test
```

`tests/conftest.py` puts the repo root on `sys.path` and installs a stub `iterm2` module (with fake `Session`/`App`/`Connection`/`util.Size`/`run_until_complete`) before anything imports `server`, because `server.py` does `import iterm2` at module level and the real package requires a running iTerm2 to even construct a connection.

## What is and isn't unit-testable

Pure functions with no iTerm2/filesystem/network dependency are fully covered: pane-identity detection (`marker_provider`, `job_provider`, `pane_provider`, `is_agent_pane`, `claude_only`), screen-scraping (`read_input_box`, `detect_input`, `detect_prompt`, `detect_mode`, `_check_state`, `_is_option_row`), text cleanup (`clean_history`, `_collapse_repeats`, `_is_spinner`), and vault helpers with no Keychain/disk dependency (`_last4`, `env_var_for`, `infer_provider`, `guess_domain`, `redact`, `_service_matches`, `_epoch`). `tests/test_imports.py` is a smoke test that every top-level module at least imports cleanly.

**Not** unit-testable without a real fleet: anything that calls into the iTerm2 API (`all_sessions`, `pane_text`, `async_send_text`, spawning/killing panes, `normalize_pane`), anything that shells out (`git_snapshot`, `_transcribe`, tailscale status), WebAuthn round-trips against a real authenticator, and the Keychain-backed vault key. Per `CONTRIBUTING.md`, exercising those requires running the server against a real iTerm2 fleet by hand and describing what you tested in the PR — there is no fixture that fakes an iTerm2 session end to end.

## How to add a route

Pick the right decorator from `server.py` (~line 1676 `guard`, ~line 1690 `writes`):

- **Read-only, needs a session**: decorate with `@guard`. It enforces the CSRF origin check (even though GETs are exempt inside `same_origin`) and the session/passkey-unlock check, then calls your handler.
- **State-changing**: decorate with `@writes("action.name")`. Same checks as `guard`, plus it parses the JSON body into `request["_body"]` for you and audits the call (truncated to 120 chars per value) before your handler runs. Use `request.get("_body") or {}` inside the handler rather than re-parsing `request.json()`.
- **If the route sends keystrokes to a pane**, it must independently call `is_agent_pane(uuid, job, text)` (re-fetching `job`/`text` fresh — never trust a client-supplied provider) before writing, and return a 403 if it fails. This is on top of `@writes`, not instead of it. See any of `api_key`/`api_send`/`api_mode` for the pattern.
- Register the route in `main()`'s `app.router.add_*` block near the end of `server.py`.

## How to add a provider

The three tracked CLIs (`claude`, `codex`, `grok`) are driven entirely by data near the top of `server.py`:

- **`PROVIDERS`** (~line 44) — add the new slug to the tuple.
- **`MARKERS`** (~line 65) — chrome strings that ONLY the new client's TUI draws itself (never a phrase that could appear in ordinary chat output, or a Claude pane discussing the new client would misclassify).
- **`job_provider`** matches on the binary name's leading word already, so a new provider whose binary is literally its slug (optionally with a `-`/`.` suffix for versioning) needs no code change there.
- If the new client writes fleet-status files in a different shape, extend `read_fleet_files`; if it keeps its own session journal (like Codex's rollout or Grok's chat log), add a case to `journal_path` and a `*_ops` incremental scanner alongside `codex_ops`/`grok_ops`, then wire it into `native_ops`.
- Claude-only controls (mode/effort/model) stay gated behind `claude_only` — don't extend those to a new provider unless it actually draws the same TUI chrome those functions scrape.

## Coding conventions

From `CONTRIBUTING.md`: match the surrounding style — terse, well-commented reasoning over abstraction. Keep changes focused. Never widen the write gate (`is_agent_pane`/pane checks) without a strong, stated reason. Never add an auth bypass that authorizes on the bootstrap token alone. Don't change the default bind off loopback. Any new on-disk secret must be added to `.gitignore` in the same change.

## PR checklist

See [CONTRIBUTING.md](../CONTRIBUTING.md) — describe what changed and why, note any security-relevant impact (auth, bind address, pane write-gating, secrets), and confirm the affected flow by hand against a real fleet if it touches the iTerm2 side.
