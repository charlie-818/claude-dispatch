# Contributing to CC Dispatch

Thanks for helping out. This project attaches to live iTerm2 Claude Code
sessions, so most of it can only be exercised on macOS with a real fleet — keep
that in mind when testing.

## Ground rules

- **Never commit secrets or per-install state.** Everything in `.gitignore`
  stays out: `.integrations.vault`, `.vault.key`, `.credentials.json`,
  `.sessions.json`, `.grants.json`, `.token`, the VAPID keypair,
  `.push_subs.json`, `audit.log`, etc. If you add a new on-disk secret, add its
  filename to `.gitignore` in the same change.
- **Loopback-first.** Don't change the default bind off `127.0.0.1`. Remote
  reach is Tailscale's job.
- **Writes stay gated.** Any new path that sends keystrokes to a pane must go
  through the existing `is_claude_pane` / `SEND_ALLOW` checks. Don't widen them
  without a strong reason.
- **Auth stays passkey-gated.** The bootstrap token is single-use for
  registration only; don't add a bypass that authorizes releases on the token
  alone.

## Getting set up

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
tailscale serve --bg 8788
.venv/bin/python server.py
```

See [README.md](README.md) for the full picture.

## Workflow

1. Fork the repo and branch from `main` (`git checkout -b my-change`).
2. Keep changes focused; match the surrounding style (the code favors terse,
   well-commented reasoning over abstraction).
3. Run the server against a real fleet and confirm the affected flow by hand —
   there's no automated suite yet, so describe what you tested in the PR.
4. Open a pull request describing **what** changed and **why**, and note any
   security-relevant impact (auth, bind address, pane write-gating, secrets).

## Reporting security issues

Please **do not** open a public issue for a vulnerability. Instead, use GitHub's
private security advisory flow (*Security → Report a vulnerability*) so it can be
handled before disclosure.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
