# Security Model

## Assets

- **The Mac itself** — the server can type into any pane it recognizes as an agent, and those panes hold whatever credentials the owner uses day to day.
- **Service credentials in the vault** (`.integrations.vault`) — GitHub tokens, cloud keys, database URLs an agent might ask for.
- **The passkey** and the session cookie it mints — the single thing standing between a phone and full control of the fleet.
- **Transcripts and fleet metadata** — session content, cost, file paths; sensitive but not directly exploitable the way a credential or a write is.

## Trust boundaries

```
   [untrusted internet] ── no listener here ──
            │
       [Tailscale / WireGuard tailnet] ── tailscale serve (TLS) ──
            │
      [phone browser / PWA] ── passkey + session cookie ──
            │
     [server.py on the Mac] ── loopback only ──
            │
      [iTerm2 panes] ── write-gated by pane identity ──
```

Each arrow is a boundary with its own control below. A stranger on the internet has nothing to reach; a stranger on the tailnet still needs the passkey; a script running in a browser tab (XSS/CSRF vector) still needs to pass the same-origin check; a compromised pane cannot escalate into the vault without an owner-approved grant.

## Controls and the code enforcing them

- **Loopback bind.** `BIND = os.environ.get("DISPATCH_BIND", "127.0.0.1")` (`server.py:33`). There is no listener on any network interface for a stranger to find; reachability is delegated entirely to `tailscale serve`, which terminates TLS and proxies to the local port. `main()` prints a loud warning if `BIND` is ever changed off loopback (`server.py:3572`).
- **Bootstrap token, single-use.** The QR/URL token (`TOKEN`, minted at boot) is accepted exactly once, at `GET /` (`server.py:1741`) or `POST /auth/bootstrap` (`auth.py`), and immediately exchanged for an `HttpOnly` session cookie via `secrets.compare_digest`. No API or WebSocket route ever inspects it again, so it can't be replayed from a URL, a screenshot, or a proxy log. Bad guesses are rate-limited per IP (`auth.note_fail`/`locked_out`, 5 per 10 minutes, 15-minute lockout).
- **Passkey (WebAuthn).** `register_begin`/`register_complete`/`login_begin`/`login_complete` (`auth.py`) implement platform-authenticator (Face ID/Touch ID) registration and login, bound to the origin (`rp_id` is the hostname only, never a bare IP — `passkey_capable` refuses IP hosts, `auth.py`), which makes it unphishable. `unlocked()` (`auth.py`) requires `level == "verified"` once any passkey is registered for the origin — a bootstrap-only session no longer passes.
- **Session lifetime.** `SESSION_TTL = 30 * 24 * 3600` (30 days absolute) and `IDLE_TTL = 7 * 24 * 3600` (drops back to `bootstrap` level after 7 days untouched) — `auth.py`. Expiry sends you back to "unlock with your passkey," never to a fresh token prompt, once a passkey exists.
- **Same-origin CSRF guard.** `auth.same_origin(request)` (`auth.py`) requires every non-GET request's `Origin` header to match the server's own derived origin; a non-browser client with no `Origin` must instead present `Sec-Fetch-Site: same-origin` (or omit it entirely, e.g. curl/tests). Enforced first, before the session check, inside both `guard()` and `writes()` (`server.py:1661`, `:1675`) — a forged cross-site POST cannot ride the cookie.
- **Write gate on panes.** Independent of the session/CSRF checks above: every keystroke-sending route re-derives the pane's live job name and screen text and calls `is_agent_pane(uuid, job, text)` (`server.py:119`) immediately before writing, refusing with 403 if the pane shows no agent UI. See [ARCHITECTURE.md](ARCHITECTURE.md#the-write-gate) for the full identity chain.
- **Vault encryption.** `.integrations.vault` is Fernet ciphertext (AES-128-CBC + HMAC); the data key lives in the macOS login Keychain (`security` CLI, item `cc-dispatch-vault`/`datakey`), not on disk (`vault.py:vault_key`). Only `vault.release()` (`vault.py:667`) ever returns a plaintext secret, and only to a pane holding a live, owner-approved grant — every other path returns `redact()`ed metadata. `/agent/*` is additionally restricted to loopback traffic with no `X-Forwarded-*` headers (`_local_agent`, `server.py:3061`), so the broker is unreachable from the tailnet even with a valid pane token.
- **Audit log.** Every gated write and every auth event is appended to `audit.log` as JSON lines (`auth.audit`, `auth.py`) — timestamp, action, ip, session level, and a truncated detail dict (secrets are never logged; releases log `cred_id`/`last4`, never the value).

## Explicitly NOT defended against

Per `SECURITY.md`:

- Compromise of the Tailnet itself (anyone who can join the tailnet and has a registered passkey is fully trusted).
- Compromise of the Mac running Dispatch (the server's own process has full pane access by design).
- An unlocked phone with the app already installed and a live session.

Additionally, from the code read for this doc: a pane once classified into `KNOWN_AGENTS` is trusted for the life of the process — identity does not expire even if the underlying job later becomes something else, by design (so a pane parked on a permission prompt stays reachable). This means a scratch shell that briefly *looked* like an agent (matched a marker string or a fleet-file provider) stays writable until the server restarts or the pane is killed.

## Reporting

See [SECURITY.md](../SECURITY.md) at the repo root — do not open a public issue; use GitHub's private security advisory flow.
