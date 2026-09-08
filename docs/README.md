# Documentation

Deeper reference for CC Dispatch, beyond the top-level [README](../README.md). Start with `ARCHITECTURE.md` if you're new to the codebase; jump straight to `API.md` or `CONFIGURATION.md` if you already know the shape and just need specifics.

- [ARCHITECTURE.md](ARCHITECTURE.md) — components, data flow, the poll loop, transcript discovery, WebSocket streams, the write gate, hot reload.
- [API.md](API.md) — every HTTP route and WebSocket message, with auth class, request/response shape.
- [CONFIGURATION.md](CONFIGURATION.md) — environment variables, on-disk state files, fleet hooks, whisper setup.
- [SECURITY-MODEL.md](SECURITY-MODEL.md) — threat model, trust boundaries, controls and the code enforcing each, what's out of scope.
- [DEPLOYMENT.md](DEPLOYMENT.md) — running by hand, launchd, auto-deploy, tailscale serve, logs.
- [DEVELOPMENT.md](DEVELOPMENT.md) — dev setup, repo layout, tests, how to add a route or a provider, conventions.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — symptom → cause → fix, grounded in the code and ops scripts.
