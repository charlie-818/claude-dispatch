# API Reference

Every route CC Dispatch exposes, its auth class, and the shape of its request/response. Routes are registered in `server.py` from line 3480 on; line numbers below point at the handler.

## Auth classes

- **public** — no cookie, no origin check. Deliberately unauthenticated (manifest/icons/ping/clientlog) or IS the auth flow itself.
- **session** (`@guard`, `server.py:1661`) — needs an unlocked session (bootstrap or verified passkey level, see `auth.unlocked`); GET-only side effects.
- **session+write-gate** (`@writes(action)`, `server.py:1675`) — same session check, plus a same-origin CSRF check on every method (not just GET), and every call is audited. Handlers under this decorator additionally re-check `is_agent_pane` before touching a pane — see [ARCHITECTURE.md](ARCHITECTURE.md#the-write-gate).
- **pane-token** — `/agent/*`, gated by loopback-only + a per-pane capability token minted at spawn (`_local_agent`, `server.py:3061`), never by the passkey session.

## HTTP routes

| Method | Path | Auth | Purpose | Request body keys | Response shape |
|---|---|---|---|---|---|
| GET | `/` | public → session | Bootstrap token exchange, then serves the shell | query `?t=` | HTML (redirect on bootstrap) |
| GET | `/api/fleet` | session | Full fleet snapshot | — | `{sessions: [...], limits: {...}}` |
| GET | `/api/summary` | session | Cached `claude -p` session summary | query `?uuid=` | `{summary, success, running, prompts}` |
| POST | `/api/key` | write-gate | Send one named key (arrows, esc, etc.) | `uuid, key` | `{ok, sent}` |
| POST | `/api/select` | write-gate | Drive a checkbox multi-select | `uuid, indices[], submit` | `{ok, toggled, submitted}` |
| POST | `/api/send` | write-gate | Send literal text, optional submit | `uuid, text, submit` | `{ok, chars}` |
| GET | `/api/browse` | session | List directories for the spawn/resume picker | query `?path=` | `{path, parent, roots, dirs}` |
| POST | `/api/upload` | session (`@guard`) | Save a phone file into the pane's cwd | headers `X-Uuid, X-Filename`; raw body | `{ok, path, name}` |
| POST | `/api/whisper` | session (`@guard`) | Transcribe recorded audio locally | raw audio body | `{ok, text}` |
| POST | `/api/submit` | write-gate | Press Enter (or retype a ghost suggestion first) | `uuid` | `{ok, kind, sent}` |
| POST | `/api/spawn` | write-gate | Open a new Claude pane | `dir?, integrations[]?` | `{uuid, dir}` |
| POST | `/api/kill` | write-gate | Gracefully quit and close a pane | `uuid` | `{ok, uuid}` |
| POST | `/api/mode` | write-gate | Shift-Tab to a permission mode (Claude only) | `uuid, mode` | `{ok, mode, path}` |
| POST | `/api/effort` | write-gate | `/effort <level>` (Claude only) | `uuid, level` | `{ok, level}` |
| POST | `/api/model` | write-gate | `/model <name>` — also rewrites the global default (Claude only) | `uuid, model` | `{ok, model, note}` |
| POST | `/api/cmd` | write-gate | Fire an allowlisted slash command | `uuid, cmd` | `{ok, cmd}` |
| GET | `/api/commands` | session | List allowlisted slash commands | — | `{commands: [{id, cmd, desc}]}` |
| GET | `/api/usage` | session | Today/30-day/all-time spend + token usage | — | `{today, daily[], all_time}` |
| GET | `/api/devices` | session (manual check) | Tailnet Macs the swapper can hop to | — | `{self, devices: [...]}` |
| GET | `/api/ping` | public, CORS-open | Cross-origin reachability probe | — | `{dispatch, host, url, name}` |
| POST | `/api/wake` | write-gate | Wake-on-LAN a sleeping tailnet Mac | `host` | `{ok, woke, sent, mac, ip, reason?, warn?}` |
| GET | `/api/history` | session | Every past top-level session transcript | — | `{history: [...]}` |
| POST | `/api/resume` | write-gate | `claude --resume <id>` in a fresh pane | `session_id` | `{uuid, session_id}` |
| GET | `/api/vapid` | session | Web-push public key | — | `{key, enabled, subs}` |
| POST | `/api/push/subscribe` | write-gate | Register a push subscription | subscription object (needs `endpoint`) | `{ok, subs}` |
| POST | `/api/push/unsubscribe` | write-gate | Drop a push subscription | `endpoint` | `{ok, subs}` |
| POST | `/api/push/test` | write-gate | Send a test push | — | `{ok, subs}` |
| GET | `/api/integrations` | session | List vault credentials (redacted) + provider registry | — | `{integrations: [...], providers: {...}}` |
| POST | `/api/integrations` | write-gate | Add a credential | `provider, secret, label?, env_var?, scopes?, expires?` | `{ok, id, integration}` |
| POST | `/api/integrations/device/start` | write-gate | Start GitHub OAuth device flow | `scopes?, label?` | `{flow, user_code, verification_uri, expires_in}` |
| POST | `/api/integrations/device/poll` | write-gate | Poll a device flow | `flow` | `{status}` or `{status:"ok", id, integration}` |
| POST | `/api/integrations/{id}/scope` | write-gate | Edit a credential's metadata (and optionally rotate its secret) | `label?, scopes?, expires?, env_var?, secret?` | `{ok, integration}` |
| POST | `/api/integrations/{id}/delete` | write-gate | Delete a credential (cascades grants) | — | `{ok}` |
| GET | `/api/requests` | session | Pending credential requests from panes | — | `{requests: [...]}` |
| POST | `/api/requests/{id}/approve` | write-gate | Approve a request into a grant | `cred_id, scopes?, expires?` | `{ok, grant}` |
| POST | `/api/requests/{id}/deny` | write-gate | Deny a request | — | `{ok}` |
| GET | `/api/grants` | session | Active grants | — | `{grants: [...]}` |
| POST | `/api/grants/{id}/revoke` | write-gate | Revoke a grant | — | `{ok}` |
| POST | `/agent/request` | pane-token | Agent asks the owner for a service credential | `token, service, reason?` | `{ok, req, status:"pending"}` |
| POST | `/agent/fetch` | pane-token | Agent redeems a granted credential | `token, service` | `{env_var, secret}` or `202 {status:"pending", req}` |
| POST | `/agent/list` | pane-token | What this pane currently holds | `token` | `{grants: [...]}` |
| GET | `/manifest.webmanifest` | public | PWA manifest | — | manifest JSON |
| GET | `/icons/{name}` | public | PWA icon | — | image |
| POST | `/api/clientlog` | public | Phone-side error → server log (no console on a phone) | `msg` | `{ok}` |
| POST | `/auth/bootstrap` | public (rate-limited) | Start a session from a pasted token | `token` | `{ok}` + `sid` cookie |
| GET | `/auth/whoami` | public (reads cookie) | Session/passkey state for the client to decide unlock vs. enrol | — | `{session, level, passkey_capable, passkey_registered, rp_id, origin, idle_ttl}` |
| POST | `/auth/register/begin` | session | Start WebAuthn registration | — | WebAuthn creation options JSON |
| POST | `/auth/register/complete` | session | Finish WebAuthn registration | WebAuthn attestation response | `{ok}` |
| POST | `/auth/login/begin` | public/session | Start WebAuthn authentication | — | WebAuthn request options JSON + `sid` cookie if fresh |
| POST | `/auth/login/complete` | session | Finish WebAuthn authentication | WebAuthn assertion response | `{ok}` |
| POST | `/auth/logout` | public (reads cookie) | Drop the session | — | `{ok}` |
| GET | `/ws/fleet` | session | Live fleet stream | — | see below |
| GET | `/ws/pane/{uuid}` | session | Live single-pane stream | — | see below |
| GET | `/sw.js` | public | Service worker | — | JS |

Handlers not decorated with `@guard`/`@writes` (`index`, `api_devices`, `ws_fleet`, `ws_pane`, `api_upload`, `api_whisper`, `api_browse`, `api_commands`) check `authed(request)` manually inline instead — same session requirement, no CSRF/audit wrapper (they're GETs or binary uploads, not JSON state changes).

## WebSocket message shapes

**`GET /ws/pane/{uuid}`** (`server.py:2966`):
- `{"gone": true}` — pane no longer exists; socket closes after.
- `{"history": [...], "cols": N}` — sent once, scrollback lines.
- `{"text": "...", "cols": N, "prompt": {...} | null, "suggest": {...} | null}` — sent on every screen change. `prompt` is `detect_prompt`'s shape (`{question, multi, options:[{index,label,key,selected,checked}]}`); `suggest` is `detect_input`'s shape (`{text, ghost}`).

**`GET /ws/fleet`** (`server.py:3002`):
- `{"sessions": [row, ...], "limits": {...}, "pending": N}` — sent on connect and whenever the serialized payload changes (roughly once a second). Each `row` matches the `build_fleet` shape documented in [ARCHITECTURE.md](ARCHITECTURE.md#the-poll-loop) (`uuid, job, provider, cwd, name, state, model, ctx, cost, effort, mode, prompt, sendable, tokens, lines_add, lines_del, files, prompts, age, dur_ms, work_since, action, subs, tail`). `pending` is `vault.pending_count()`.

## Error conventions

- **401** — no/invalid session (`{"error": "no session"}`) or a session that's dropped back to `bootstrap` level while a passkey is registered (`{"error": "locked", "relock": true}`).
- **403** — CSRF origin mismatch (`{"error": "bad origin"}`) or the write-gate refusing a pane with no agent UI (`{"error": "pane is running '<job>' and shows no agent UI — refusing"}`), or a loopback-only route reached cross-tailnet.
- **404** — no such pane/request/grant/credential/session.
- **409** — a Claude-only control refused because the pane runs a different provider, or a mode change that couldn't reach the requested state.
- **429** — bootstrap token rate limit (`auth.locked_out`).
- **500/502/504** — subprocess/spawn failures, upstream GitHub device-flow errors, whisper timeout.
- **503** — whisper model missing on disk.
