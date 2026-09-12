"""The maximize/restore bookkeeping behind the pane view.

A maximized pane hides its tab-mates from `tab.sessions`; they are still in
`tab.all_sessions` and still readable. These pin the two consequences:
hidden panes stay reachable, and a maximize nobody owns gets noticed.
"""
import asyncio
import types

import server as srv


class _S:
    def __init__(self, sid, grid=None):
        self.session_id = sid
        self.grid_size = grid


class _T:
    def __init__(self, tab_id, visible, minimized):
        self.tab_id = tab_id
        self.sessions = visible
        self.minimized_sessions = minimized
        self.all_sessions = visible + minimized


class _W:
    def __init__(self, wid, tabs):
        self.window_id = wid
        self.tabs = tabs


class _App:
    def __init__(self, windows):
        self.terminal_windows = windows

    async def async_refresh(self):
        pass


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
