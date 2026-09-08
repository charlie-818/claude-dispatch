# Launchd Deployment

CC Dispatch can be run as a macOS launchd agent for persistent background execution.

## Installation

Run the install script from the repository root:

```bash
deploy/install-launchd.sh [ensure|server|deploy|all]
```

The default is `ensure`.

## Jobs

### `com.ccdispatch.ensure` (recommended)

A watchdog that opens iTerm2 and starts the server inside it. This is the recommended configuration because the server needs the `ITERM2_COOKIE` environment variable and hangs when launched headless (without a terminal).

Install with: `deploy/install-launchd.sh ensure`

### `com.ccdispatch`

Runs the server directly under launchd with `KeepAlive: true`. This only works if iTerm2 has granted API access to launchd-spawned processes — which is unusual. Use this only if you've explicitly enabled such access.

Install with: `deploy/install-launchd.sh server`

### `com.ccdispatch.deploy`

An auto-mirror for a deploy target. Polls origin/main and runs `git reset --hard` to stay in sync. Useful for maintaining multiple instances.

Install with: `deploy/install-launchd.sh deploy`

### Install all three

```bash
deploy/install-launchd.sh all
```

## How it works

The install script:

1. Reads the template plist for the requested job(s)
2. Substitutes `__REPO__` with the repository's absolute path
3. Copies the result to `~/Library/LaunchAgents/`
4. Unloads any existing version (errors ignored)
5. Loads the new version with `launchctl load`

## Logs

Output is redirected to files in the repository root:

- `ensure.out` — from `com.ccdispatch.ensure`
- `server.out` — from `com.ccdispatch`
- `deploy.out` — from `com.ccdispatch.deploy`
