"""New agent panes stack full width, but never below PANE_ROWS each: a 7-row
Claude pane spills every repaint into scrollback (duplicated chat lines)."""
from types import SimpleNamespace

import iterm2

import server as srv


def _pane(h):
    s = iterm2.Session.__new__(iterm2.Session)
    s.__dict__["grid_size"] = SimpleNamespace(width=214, height=h)
    return s


def _tab(*heights, vertical=False):
    if len(heights) == 1:
        return SimpleNamespace(root=_pane(heights[0]))
    kids = [_pane(h) for h in heights]
    return SimpleNamespace(root=SimpleNamespace(vertical=vertical, children=kids))


def test_tall_single_pane_is_split():
    t = _tab(55)
    assert srv._stack_slot(t) is t.root


def test_tallest_row_is_split_when_room():
    t = _tab(20, 60, 30)
    assert srv._stack_slot(t) is t.root.children[1]


def test_full_tab_is_not_split_further():
    assert srv._stack_slot(_tab(27, 27)) is None
    assert srv._stack_slot(_tab(7, 7, 7, 7, 7, 7)) is None
    assert srv._stack_slot(_tab(40)) is None


def test_side_by_side_tab_is_not_split():
    assert srv._stack_slot(_tab(60, 60, vertical=True)) is None
