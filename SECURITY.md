# Security Policy

## Supported Versions

Security updates are provided only for the `main` branch. We recommend always running the latest version.

## Reporting a Vulnerability

**Do not open public issues for security vulnerabilities.** Instead, please report them using GitHub Security Advisories:

https://github.com/charlie-818/claude-dispatch/security/advisories/new

We will acknowledge reports within 7 days and work with you to resolve them.

## Security Model

CC Dispatch's security depends on:

- **Loopback binding** — the server binds to `127.0.0.1` by default, reachable only via Tailscale's `tailscale serve`.
- **Passkey gating** — registration requires a one-time bootstrap token and a real TLS origin (the Tailnet host).
- **Pane write-gating** — only panes with confirmed Claude/Codex/Grok foreground jobs, or recent fleet-status writers, can receive keystrokes.
- **Encrypted vault** — integration secrets are stored encrypted (`.integrations.vault`); the Fernet key lives in the macOS login keychain.

## Out of Scope

We do not defend against:
- Compromise of the Tailnet itself
- Compromise of the Mac running dispatch
- An unlocked phone with the app installed
