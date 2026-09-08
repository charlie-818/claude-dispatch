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
# make
make venv
tailscale serve --bg 8788
make run

# raw
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
tailscale serve --bg 8788
.venv/bin/python server.py
```

See [README.md](README.md) for the full picture.

## What to work on

Look for issues labelled `good first issue` or `help wanted`. For anything
that touches auth, bind address, or the write gate, open a feature request
first so the approach can be agreed before you write code.

## Project layout

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) rather than duplicating it here.

## Workflow

1. Fork the repo and branch from `main` (`git checkout -b my-change`).
2. Keep changes focused; match the surrounding style (the code favors terse,
   well-commented reasoning over abstraction).
3. Run `make check` (ruff + pytest) — it passes offline; then exercise the
   affected flow against a real fleet by hand and describe it in the PR (the
   PR template asks).
4. Open a pull request describing **what** changed and **why**, and note any
   security-relevant impact (auth, bind address, pane write-gating, secrets).

## Style

Ruff config lives in `pyproject.toml`. Terse one-liners are house style, so
`E701`/`E702`/`E741`/`E401` are deliberately off — don't "fix" them. Comments
should explain *why*, not restate the code. Don't add new abstraction layers;
match what's already there.

## Tests

Add unit tests under `tests/` for any new pure parser or gate logic. Stub
iTerm2 via `conftest.py` rather than talking to a real session — the suite has
to run in CI (ubuntu + macOS, py3.11/3.12) with no live fleet available.

## Docs

Keep docs in sync with the change, not as an afterthought:

- Route changed or added → update [docs/API.md](docs/API.md).
- New env var or on-disk file → update [docs/CONFIGURATION.md](docs/CONFIGURATION.md).
- User-visible change → add a line under `Unreleased` in [CHANGELOG.md](CHANGELOG.md).

## Commits & PRs

- Imperative subject line (`Fix pane detection for Codex rollouts`, not `Fixed`
  or `Fixes`); put the *why* in the body.
- One topic per PR — don't bundle an unrelated refactor with a fix.
- Call out any security-relevant impact explicitly (auth, bind address, write
  gating, secrets) even if it seems minor.

## Reporting security issues

Please **do not** open a public issue for a vulnerability. Instead, use GitHub's
private security advisory flow (*Security → Report a vulnerability*) so it can be
handled before disclosure. See [SECURITY.md](SECURITY.md) for details.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
</content>
