"""The phone's wrap-mode pane (static/index.html) draws one .ln block per
terminal row. Claude Code hard-wraps its own prose with real newlines at the
terminal's column count (214 in these fixtures), so on a much narrower phone
each of those rows gets soft-wrapped again on top — ragged mid-sentence
breaks, even mid-word splits, one word per row.

reflowRows merges a hard-wrapped row back into the logical line it broke out
of before the phone lays it out, so the phone's own soft-wrap breaks at word
boundaries instead. Runs the real cellw/reflowRows from index.html in node on
rows copied verbatim from captured panes (frame-*.json in the scratchpad).
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "static" / "index.html").read_text()

# table rows from the ~/tokenized-staking pane, reused from test_table_grid —
# box-drawing rows must never be reflowed.
TABLE = [
    "  ┌────────────┬──────────────┬───────────┬─────────────┬───────────┐",
    "  │   Width    │ Median $/day │ p25 $/day │ 7-day total │ Recentres │",
    "  ├────────────┼──────────────┼───────────┼─────────────┼───────────┤",
    "  │ ±10% (now) │ 15.2         │ 11.0      │ $108        │ 0         │",
    "  └────────────┴──────────────┴───────────┴─────────────┴───────────┘",
]

RULE = ["─" * 214]

# captured verbatim from frame-09C21256.json (history rows 575-590), 214
# cols: one hard-wrapped sentence split mid-word ("same le" | "ak") and then
# one word per row, ending with a blank line.
FRAME_09C21256 = [
    "  That $150 slide is a real loss. Fees are still coming in, about $130 since Oct 31, but losses to arbitrage traders and the gap between the pool price and Hyperliquid have taken more than that. This is the same le",
    "ak", "found", "in", "the", "Oct", "20–22", "P&L", "breakdowns,", "and",
    "nothing", "has", "fixed", "it", "yet.", "",
]
FRAME_09C21256_EXPECT = [
    "  That $150 slide is a real loss. Fees are still coming in, about $130 since Oct 31, but losses to arbitrage traders and the gap between the pool price and Hyperliquid have taken more than that. This is the same leak found in the Oct 20–22 P&L breakdowns, and nothing has fixed it yet.",
    "",
]

# captured verbatim from frame-87074740.json (history rows 34-37), 214 cols:
# a normal paragraph wrap with a 2-space hanging indent — joins with one
# space.
FRAME_87074740_JOIN = [
    "  What Claude Code records: each background agent writes its own log under ~/.claude/projects/…/<chat-id>/subagents/. It holds the agent's prompt, every tool call and its output, pass or fail for each call, the",
    "  model and token usage. When an agent finishes, a notice lands in the parent chat's log with its status, final result, tool-call count and duration. The monitor reads both of these and only ever reads.",
    "",
    "  How it works:",
]
FRAME_87074740_JOIN_EXPECT = [
    "  What Claude Code records: each background agent writes its own log under ~/.claude/projects/…/<chat-id>/subagents/. It holds the agent's prompt, every tool call and its output, pass or fail for each call, the model and token usage. When an agent finishes, a notice lands in the parent chat's log with its status, final result, tool-call count and duration. The monitor reads both of these and only ever reads.",
    "",
    "  How it works:",
]

# negative: rows 66-68 of the same frame — a numbered list item that happens
# to run the full row width must not swallow the next numbered item.
FRAME_87074740_NUMBERED = [
    "  1. Detail view: more room at the top.",
    "  2. Agent list: a stacked card for each agent (description on top, then a small grey line with type · status · time and a status dot), instead of text squeezed into a left and a right column.",
    "  3. What the agent did, built from its log:",
]

# negative: rows 99-112 of the same frame — a unified-diff style dump with
# line-number columns ("      419 ", "      420 ") must not be reflowed; the
# plain digit-then-space test in startsNew is what protects it.
FRAME_87074740_DIFF = [
    "  ⎿ \xa0Updated static/index.html (+5 -0)",
    "      419      white-space:nowrap;overflow:hidden;text-overflow:ellipsis;",
    "      420      transform:translateY(var(--chips-h,0px));box-shadow:0 -1px 0 var(--bg)}",
    "      421    .panestatus[hidden]{display:none}",
]

# captured verbatim from frame-BAFEF0B4.json (text rows 498-515), 214 cols: a
# paragraph cut exactly at the column with no indent at all on the
# continuation ("...Then I" | "'ll" | "make" | "the" | ...) — the
# one-word-per-row artifact the diagnosis describes.
FRAME_BAFEF0B4 = [
    "  A scout is finding the chat view and its summary box: the animation code, and what decides whether the summary is already loaded or cached. If it isn't in this repo, the scout will list other likely repos. Then I",
    "'ll", "make", "the", "animation", "run", "only", "the", "first", "time",
    "the", "summary", "appears,", "and", "show", "it", "instantly",
]
FRAME_BAFEF0B4_EXPECT_HEAD = (
    "  A scout is finding the chat view and its summary box: the animation code, and what decides whether the summary is already loaded or cached. If it isn't in this repo, the scout will list other likely repos. Then I'll make the animation run only the first time the summary appears, and show it instantly"
)


def _js():
    def grab(start, end):
        i = HTML.index(start)
        return HTML[i:HTML.index(end, i)]
    return grab("function cellw", "\n/* ── tables")


def _run(rows, cols, start=0):
    script = _js() + """
console.log(JSON.stringify(reflowRows(%s, %d, %d)));
""" % (json.dumps(rows), cols, start)
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_mid_word_hard_wrap_rejoins_into_one_sentence():
    r = _run(FRAME_09C21256, 214)
    assert r["rows"] == FRAME_09C21256_EXPECT


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_hanging_indent_continuation_joins_with_one_space():
    r = _run(FRAME_87074740_JOIN, 214)
    assert r["rows"] == FRAME_87074740_JOIN_EXPECT


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_numbered_list_rows_are_not_joined():
    r = _run(FRAME_87074740_NUMBERED, 214)
    assert r["rows"] == FRAME_87074740_NUMBERED


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_diff_line_numbers_are_not_joined():
    r = _run(FRAME_87074740_DIFF, 214)
    assert r["rows"] == FRAME_87074740_DIFF


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_one_word_per_row_chain_rejoins():
    r = _run(FRAME_BAFEF0B4, 214)
    assert len(r["rows"]) == 1
    assert r["rows"][0] == FRAME_BAFEF0B4_EXPECT_HEAD


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_table_rows_are_never_joined():
    r = _run(TABLE, 214)
    assert r["rows"] == TABLE


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_rule_row_is_never_joined():
    rows = TABLE[:2] + RULE + ["  next paragraph"]
    r = _run(rows, 214)
    assert r["rows"] == rows


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_from_maps_to_the_live_rows_after_a_history_merge():
    # 3 history rows (untouched), then a live paragraph that gets cut exactly
    # at the column and continues as one word per row.
    hist = [
        "  - Nov 2 to Nov 4: the graph showed about $1,100 because of the missing deposit. Then the $820 move into the Squads vault was wrongly shown as a loss, which is why it fell to about $250.",
        "  - The real trend: down about $150 since Oct 20, from +$773 to +$617.",
        "",
    ]
    live = [
        "  To be sure this doesn't happen again, I can add a check to the hourly rebuild. Any jump in Net P&L over $150 that doesn't match fees or a recorded deposit would get flagged on Telegram before anyone relies on it.",
        "Want", "that?",
    ]
    r = _run(hist + live, 214, len(hist))
    assert r["rows"][:len(hist)] == hist
    assert r["from"] == len(hist)
    assert len(r["rows"]) == len(hist) + 1
    # a full row that ended a sentence keeps its space when the next row joins
    assert r["rows"][-1].endswith("relies on it. Want that?")


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_from_is_rows_length_when_start_is_past_the_end():
    r = _run(FRAME_87074740_NUMBERED, 214, 1000)
    assert r["from"] == len(r["rows"])


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_cellw_counts_wide_glyphs_as_two_cells():
    script = _js() + "console.log(JSON.stringify([cellw('abc'), cellw('你好'), cellw('a你b')]));"
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(res.stdout) == [3, 4, 4]
