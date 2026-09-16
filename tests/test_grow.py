"""The maximize/restore bookkeeping behind the pane view.

A maximized pane hides its tab-mates from `tab.sessions`; they are still in
`tab.all_sessions` and still readable. These pin the two consequences:
hidden panes stay reachable, and a maximize nobody owns gets noticed.
"""
import asyncio
import time
import types

import server as srv


class _S:
    def __init__(self, sid, grid=None):
        self.session_id = sid
        self.grid_size = grid
        self.activated = 0
        self.tab = None

    async def async_activate(self, select_tab=True, order_window_front=False):
        self.activated += 1
        if self.tab is not None:
            self.tab.active_session_id = self.session_id


class _T:
    def __init__(self, tab_id, visible, minimized):
        self.tab_id = tab_id
        self.sessions = visible
        self.minimized_sessions = minimized
        self.all_sessions = visible + minimized
        self.active_session_id = visible[0].session_id if visible else None


class _W:
    def __init__(self, wid, tabs):
        self.window_id = wid
        self.tabs = tabs
        self.activated = 0

    async def async_activate(self):
        self.activated += 1
        if srv.APP is not None:
            srv.APP.current_window = self


class _App:
    def __init__(self, windows):
        self.terminal_windows = windows
        self.current_window = windows[0] if windows else None

    async def async_refresh(self):
        pass


def _reset_grow(monkeypatch, grace=0):
    monkeypatch.setattr(srv, "_GROWN", None)
    monkeypatch.setattr(srv, "_WATCHERS", {})
    monkeypatch.setattr(srv, "_UNGROW_TRIES", 0)
    monkeypatch.setattr(srv, "_RESTORE_AFTER", {})
    monkeypatch.setattr(srv, "_GROW_FOCUS_TRIES", 0)
    monkeypatch.setattr(srv, "_GROW_FOCUS_RETRY_AT", 0.0)
    monkeypatch.setattr(srv, "_GROW_TRIED", set())
    monkeypatch.setattr(srv, "_RESTORE_GRACE", grace)
    monkeypatch.setattr(srv, "_GROW_LOCK", asyncio.Lock())


def _grow_env(monkeypatch, provider="grok", sid="AAAA", other="BBBB",
              key=True, grace=0, known=None):
    s1, s2 = _S(sid), _S(other)
    tab = _T("2", [s1, s2], [])
    s1.tab = s2.tab = tab
    w = _W("w1", [tab])
    app = _App([w])
    if not key:
        app.current_window = _W("other", [])
    monkeypatch.setattr(srv, "APP", app)
    monkeypatch.setattr(srv, "KNOWN_AGENTS",
                        known if known is not None
                        else {sid: provider, other: "claude"})
    state = {"on": False, "toggles": 0}

    async def fake_max():
        return state["on"]

    async def fake_toggle():
        state["on"] = not state["on"]
        state["toggles"] += 1
        if state["on"]:
            aid = tab.active_session_id
            vis = next((s for s in tab.all_sessions if s.session_id == aid),
                       tab.all_sessions[0])
            tab.sessions = [vis]
            tab.minimized_sessions = [s for s in tab.all_sessions if s is not vis]
        else:
            tab.sessions = list(tab.all_sessions)
            tab.minimized_sessions = []

    monkeypatch.setattr(srv, "_maximized", fake_max)
    monkeypatch.setattr(srv, "_toggle_maximize", fake_toggle)
    _reset_grow(monkeypatch, grace=grace)
    return types.SimpleNamespace(
        sess=s1, other=s2, tab=tab, win=w, app=app, state=state,
        sid=sid, other_id=other)


def _fleet(monkeypatch, visible, minimized, known=True):
    tab = _T("2", visible, minimized)
    app = _App([_W("w1", [tab])])
    monkeypatch.setattr(srv, "APP", app)
    monkeypatch.setattr(srv, "KNOWN_AGENTS",
                        {s.session_id: "claude" for s in tab.all_sessions}
                        if known else {})
    return tab


def test_all_sessions_includes_minimized(monkeypatch):
    _fleet(monkeypatch, [_S("AAAA")], [_S("BBBB"), _S("CCCC")])
    got = asyncio.run(srv.all_sessions())
    assert set(got) == {"AAAA", "BBBB", "CCCC"}


def test_locate_finds_minimized(monkeypatch):
    _fleet(monkeypatch, [_S("AAAA")], [_S("BBBB")])
    w, t, s = srv._locate("bbbb")
    assert s is not None and s.session_id == "BBBB"


def test_orphan_maximize_detected(monkeypatch):
    monkeypatch.setattr(srv, "_GROWN", None)
    _fleet(monkeypatch, [_S("AAAA")], [_S("BBBB")])
    assert srv._orphan_maximize() == ("AAAA", "w1")


def test_orphan_ignores_unmanaged_and_flat(monkeypatch):
    _fleet(monkeypatch, [_S("AAAA")], [_S("BBBB")], known=False)
    assert srv._orphan_maximize() is None
    _fleet(monkeypatch, [_S("AAAA"), _S("BBBB")], [])
    assert srv._orphan_maximize() is None


def test_pane_cols_falls_back_to_last_seen(monkeypatch):
    monkeypatch.setattr(srv, "_COLS_SEEN", {})
    s = _S("DDDD", grid=types.SimpleNamespace(width=52, height=25))
    assert asyncio.run(srv.pane_cols(s)) == 52
    s.grid_size = None                      # now minimized
    assert asyncio.run(srv.pane_cols(s)) == 52


def test_pane_history_streams_growth():
    class _Line:
        def __init__(self, t): self.string = t

    class _Sess:
        sb = 10
        async def async_get_line_info(self):
            return types.SimpleNamespace(overflow=100, scrollback_buffer_height=self.sb)
        async def async_get_contents(self, first, n):
            return [_Line(f"line {first + i}") for i in range(n)]

    s = _Sess()
    lines, end = asyncio.run(srv.pane_history(s))
    assert lines[0] == "line 100" and lines[-1] == "line 109" and end == 110
    more, end2 = asyncio.run(srv.pane_history(s, since=end))
    assert more == [] and end2 == 110          # nothing scrolled off yet
    s.sb = 13
    more, end3 = asyncio.run(srv.pane_history(s, since=end))
    assert more == ["line 110", "line 111", "line 112"] and end3 == 113


def test_grow_pane_maximizes_grok(monkeypatch):
    env = _grow_env(monkeypatch, provider="grok")
    asyncio.run(srv.grow_pane(env.sid))
    assert srv._GROWN is None
    assert env.state["toggles"] == 0
    assert env.sid in srv._WATCHERS


def test_grow_pane_skips_claude(monkeypatch):
    env = _grow_env(monkeypatch, provider="claude")
    asyncio.run(srv.grow_pane(env.sid))
    assert srv._GROWN is None
    assert env.state["toggles"] == 0


def test_ungrow_pane_restores(monkeypatch):
    env = _grow_env(monkeypatch, provider="grok", grace=0)

    async def go():
        await srv.grow_pane(env.sid)
        assert env.sid in srv._WATCHERS
        await srv.ungrow_pane(env.sid)

    asyncio.run(go())
    assert env.sid not in srv._WATCHERS
    assert srv._GROWN is None
    assert env.state["toggles"] == 0


def test_reconnect_cancels_restore(monkeypatch):
    env = _grow_env(monkeypatch, provider="grok", grace=0.05)

    async def go():
        await srv.grow_pane(env.sid)
        t = asyncio.create_task(srv.ungrow_pane(env.sid))
        while env.sid in srv._WATCHERS:
            await asyncio.sleep(0)
        await srv.grow_pane(env.sid)
        await t
        assert srv._WATCHERS.get(env.sid, 0) == 1
        assert srv._GROWN is None
        assert env.state["toggles"] == 0

    asyncio.run(go())


def test_ungrow_two_watchers(monkeypatch):
    env = _grow_env(monkeypatch, provider="grok", grace=0)

    async def go():
        await srv.grow_pane(env.sid)
        await srv.grow_pane(env.sid)
        await srv.ungrow_pane(env.sid)
        assert srv._WATCHERS[env.sid] == 1
        assert srv._GROWN is None
        await srv.ungrow_pane(env.sid)
        assert env.sid not in srv._WATCHERS
        assert srv._GROWN is None and env.state["toggles"] == 0

    asyncio.run(go())


def test_grow_tick_keeps_watched_grok(monkeypatch):
    env = _grow_env(monkeypatch, provider="grok")

    async def go():
        await srv.grow_pane(env.sid)
        n = env.state["toggles"]
        await srv._grow_tick()
        assert srv._GROWN is None
        assert env.state["toggles"] == n == 0

    asyncio.run(go())


def test_grow_tick_ungrows_unwatched_grok(monkeypatch):
    env = _grow_env(monkeypatch, provider="grok")
    env.tab.sessions = [env.sess]
    env.tab.minimized_sessions = [env.other]
    env.tab.all_sessions = [env.sess, env.other]
    env.state["on"] = True
    srv._GROWN = (env.sid, None, env.win.window_id)

    async def go():
        srv._WATCHERS.pop(env.sid, None)
        srv._RESTORE_AFTER.clear()
        await srv._grow_tick()
        assert srv._GROWN is None and env.state["on"] is False

    asyncio.run(go())


def test_grow_tick_respects_restore_grace(monkeypatch):
    env = _grow_env(monkeypatch, provider="grok")
    env.state["on"] = True
    srv._GROWN = (env.sid, None, env.win.window_id)

    async def go():
        srv._WATCHERS.pop(env.sid, None)
        srv._RESTORE_AFTER[env.sid] = time.monotonic() + 10
        await srv._grow_tick()
        assert srv._GROWN[0] == env.sid and env.state["on"] is True

    asyncio.run(go())


def test_grow_tick_grows_watched_grok(monkeypatch):
    env = _grow_env(monkeypatch, provider="grok")
    srv._WATCHERS[env.sid] = 1
    env.tab.sessions = [env.sess]
    env.tab.minimized_sessions = [env.other]
    env.tab.all_sessions = [env.sess, env.other]
    env.state["on"] = True
    asyncio.run(srv._grow_tick())
    assert srv._GROWN is None
    assert env.state["toggles"] == 1 and env.state["on"] is False


def test_grow_tick_does_not_toggle_to_grow(monkeypatch):
    env = _grow_env(monkeypatch, provider="grok")
    srv._WATCHERS[env.sid] = 1
    asyncio.run(srv._grow_tick())
    assert srv._GROWN is None
    assert env.state["toggles"] == 0


def test_grow_tick_restores_orphan_claude(monkeypatch):
    env = _grow_env(monkeypatch, provider="claude")
    env.tab.sessions = [env.sess]
    env.tab.minimized_sessions = [env.other]
    env.tab.all_sessions = [env.sess, env.other]
    env.state["on"] = True
    asyncio.run(srv._grow_tick())
    assert srv._GROWN is None
    assert env.state["toggles"] == 1 and env.state["on"] is False


def test_grow_activates_non_key_window(monkeypatch):
    env = _grow_env(monkeypatch, provider="grok", key=False)
    asyncio.run(srv.grow_pane(env.sid))
    assert env.win.activated == 0
    assert srv._GROWN is None and env.state["on"] is False


def test_grow_pane_switches_grok(monkeypatch):
    env = _grow_env(monkeypatch, provider="grok")
    srv.KNOWN_AGENTS[env.other_id] = "grok"

    async def go():
        await srv.grow_pane(env.sid)
        await srv.grow_pane(env.other_id)
        assert srv._GROWN is None
        assert env.state["toggles"] == 0
        assert srv._WATCHERS[env.sid] == 1
        assert srv._WATCHERS[env.other_id] == 1

    asyncio.run(go())


def test_maybe_grow_does_not_steal(monkeypatch):
    env = _grow_env(monkeypatch, provider="grok")
    srv.KNOWN_AGENTS[env.other_id] = "grok"

    async def go():
        await srv.grow_pane(env.sid)
        srv._WATCHERS[env.other_id] = 1
        await srv._maybe_grow(env.other_id, "grok")
        assert srv._GROWN is None
        assert env.state["toggles"] == 0
        await srv._grow_tick()
        assert srv._GROWN is None

    asyncio.run(go())


def test_grow_tick_does_not_reattach_lost_maximize(monkeypatch):
    env = _grow_env(monkeypatch, provider="grok")

    async def go():
        await srv.grow_pane(env.sid)
        n = env.state["toggles"]
        env.state["on"] = False
        await srv._grow_tick()
        assert env.state["toggles"] == n == 0
        assert srv._GROWN is None

    asyncio.run(go())


def test_maybe_grow_grok_only_with_watchers(monkeypatch):
    env = _grow_env(monkeypatch, provider="claude")
    srv._WATCHERS[env.sid] = 1
    asyncio.run(srv._maybe_grow(env.sid, "claude"))
    assert srv._GROWN is None and env.state["toggles"] == 0

    env = _grow_env(monkeypatch, provider="grok")
    srv._WATCHERS[env.sid] = 1
    asyncio.run(srv._maybe_grow(env.sid, "grok"))
    assert srv._GROWN is None and env.state["toggles"] == 0


class _FSWin(_W):
    """Window that remembers whether it is in macOS native full screen."""

    def __init__(self, wid, tabs, fullscreen):
        super().__init__(wid, tabs)
        self.fullscreen = fullscreen
        self.exits = 0

    async def async_get_fullscreen(self):
        return self.fullscreen

    async def async_set_fullscreen(self, value):
        self.fullscreen = value
        self.exits += 1


def _fs_env(monkeypatch, fullscreen, known=True):
    """One agent window in the given full-screen state, plus a bystander."""
    agent = _FSWin("agent", [_T("1", [_S("AAAA")], [])], fullscreen)
    other = _FSWin("other", [_T("2", [_S("ZZZZ")], [])], True)
    monkeypatch.setattr(srv, "APP", _App([agent, other]))
    monkeypatch.setattr(srv, "KNOWN_AGENTS", {"AAAA": "claude"} if known else {})
    monkeypatch.setattr(srv.asyncio, "sleep", _no_sleep)
    return agent, other


async def _no_sleep(_seconds):
    return None


def test_unfullscreen_frees_the_agent_window(monkeypatch):
    # A full-screen window refuses every grid resize, so normalize can never pin
    # PANE_COLS there — dropping it out of full screen is what unblocks retiling.
    agent, other = _fs_env(monkeypatch, fullscreen=True)
    asyncio.run(srv._unfullscreen_agent_windows())
    assert agent.fullscreen is False
    assert agent.exits == 1
    assert other.fullscreen is True          # no agents in it: left alone
    assert other.exits == 0


def test_unfullscreen_leaves_a_windowed_window_alone(monkeypatch):
    agent, _ = _fs_env(monkeypatch, fullscreen=False)
    asyncio.run(srv._unfullscreen_agent_windows())
    assert agent.exits == 0


def test_unfullscreen_skips_windows_without_agents(monkeypatch):
    agent, other = _fs_env(monkeypatch, fullscreen=True, known=False)
    asyncio.run(srv._unfullscreen_agent_windows())
    assert agent.exits == 0
    assert other.exits == 0


def test_normalize_all_frees_full_screen_first(monkeypatch):
    calls = []

    async def fake_unfull():
        calls.append("unfull")

    async def fake_sessions():
        calls.append("scan")
        return {}

    monkeypatch.setattr(srv, "_unfullscreen_agent_windows", fake_unfull)
    monkeypatch.setattr(srv, "all_sessions", fake_sessions)
    monkeypatch.setattr(srv, "KNOWN_AGENTS", {})
    asyncio.run(srv.normalize_all(passes=1))
    assert calls[0] == "unfull"
