"""/api/wake at the HTTP level: routing, host validation, and the contract that a
Mac which fails to wake is a 200 with the reason in the body, not a transport error."""
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import auth
import server

DEVICES = [{"host": "sleeper.example.ts.net", "self": False, "online": False, "name": "sleeper"},
           {"host": "me.example.ts.net", "self": True, "online": True, "name": "me"}]


async def _client(monkeypatch, wake_result=None):
    # The endpoint is behind writes(): same-origin + an unlocked session + audit.
    monkeypatch.setattr(auth, "same_origin", lambda r: True)
    monkeypatch.setattr(auth, "unlocked", lambda r: {"level": "verified"})
    monkeypatch.setattr(auth, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "tailnet_macs", lambda: DEVICES)
    if wake_result is not None:
        monkeypatch.setattr(server.wake, "wake", lambda host: wake_result)
    app = web.Application()
    app.router.add_post("/api/wake", server.api_wake)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_wake_rejects_a_host_not_on_the_tailnet(monkeypatch):
    client = await _client(monkeypatch, {"ok": True})
    try:
        r = await client.post("/api/wake", json={"host": "stranger.example.ts.net"})
        assert r.status == 404
    finally:
        await client.close()


async def test_wake_rejects_a_missing_host(monkeypatch):
    client = await _client(monkeypatch, {"ok": True})
    try:
        r = await client.post("/api/wake", json={})
        assert r.status == 400
    finally:
        await client.close()


async def test_wake_that_does_not_land_is_200_with_a_reason(monkeypatch):
    # A non-2xx here would make the client's post() helper throw the explanation
    # away, leaving the UI with nothing to show.
    result = {"ok": True, "woke": False, "sent": 18, "reason": "did not answer in 90s"}
    client = await _client(monkeypatch, result)
    try:
        r = await client.post("/api/wake", json={"host": "sleeper.example.ts.net"})
        assert r.status == 200
        body = await r.json()
        assert body["woke"] is False
        assert "did not answer" in body["reason"]
    finally:
        await client.close()


async def test_wake_passes_the_host_through(monkeypatch):
    seen = {}

    def fake_wake(host):
        seen["host"] = host
        return {"ok": True, "woke": True, "sent": 18}

    client = await _client(monkeypatch, None)
    monkeypatch.setattr(server.wake, "wake", fake_wake)
    try:
        r = await client.post("/api/wake", json={"host": "sleeper.example.ts.net."})
        assert r.status == 200
        assert seen["host"] == "sleeper.example.ts.net"   # trailing dot stripped
    finally:
        await client.close()
