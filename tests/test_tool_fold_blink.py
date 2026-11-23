"""A running Claude tool blinks its ⏺ off to a blank cell every 600ms. In
compact view a header folds its output, so a header that stops being
recognised on the off beat unfolds and refolds on its own. Runs the real
blink/clean/collapseTools code from index.html in node on both beats."""
import json
import pathlib
import shutil
import subprocess

import pytest

HTML = (pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()


def _grab(start, end):
    i = HTML.index(start)
    return HTML[i:HTML.index(end, i)]


def _js():
    return "\n".join([
        _grab("const STATUS_LINE =", "const KEY_HINT"),
        _grab("const DONE_LINE =", "/* Per-section token totals"),
        _grab("const BOX_ROW  =", "function isBoxRow"),
        _grab("const SMEAR =", "/* Compact view (chips row)"),
        "const openTools = new Set();",
        _grab("const TOOL_DOT =", "/* Rows of each sent prompt"),
        _grab("const BLINK_OFF =", "let paintPending"),
    ])


def _fold(rows):
    """paint()'s claude path up to collapseTools, compact view on. Returns
    [heads as [row, key, body rows], rows after folding]."""
    script = _js() + r"""
let raw = %s;
raw = raw.map((l, i, a) => blinkedOff(a, i) ? '' + l.slice(1) : l);
raw = cleanCapture(bannerize(raw));
const heads = collapseTools(raw, true);
console.log(JSON.stringify([[...heads].map(([h, v]) => [h, v[0], v[1]]), raw]));
""" % json.dumps(rows)
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


# (on-beat header, off-beat header, one-line running body). Claude Code draws
# the off beat as a blank in the bullet's 2-col box.
RUNNING = [
    ('⏺ context7 - Query Docs (MCP)(libraryId: "/vercel/next.js")',
     '  context7 - Query Docs (MCP)(libraryId: "/vercel/next.js")', "  ⎿ \xa0Running…"),
    ('⏺ Web Search("iterm2 python api")', '  Web Search("iterm2 python api")', "  ⎿ \xa0Searching…"),
    ("⏺ Claude in Chrome[navigate](https://x.dev)", "  Claude in Chrome[navigate](https://x.dev)",
     "  ⎿ \xa0Running…"),
    ("⏺ scout(Locate collapseTools callers)", "  scout(Locate collapseTools callers)", "  ⎿ \xa0Initializing…"),
    ("⏺ Reading 1 file… (ctrl+o to expand)", "  Reading 1 file… (ctrl+o to expand)", "  ⎿ \xa0static/index.html"),
    ("⏺ Bash(sleep 30)", "  Bash(sleep 30)", "  ⎿ \xa0Running… (3s)"),
]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("on,off,body", RUNNING)
def test_running_header_stays_folded_on_both_blink_beats(on, off, body):
    heads_on, rows_on = _fold(["", on, body])
    heads_off, rows_off = _fold(["", off, body])
    assert heads_on == heads_off == [[1, on[2:] + "#1", 1]]
    assert rows_on[2] == rows_off[2] == ""


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_multi_line_body_unchanged():
    body = ["  ⎿ \xa0PASS a.test.ts", "     PASS b.test.ts", "     +12 lines (5s)"]
    for head in ("⏺ Bash(npm test)", "  Bash(npm test)"):
        heads, rows = _fold(["", head] + body)
        assert heads == [[1, "Bash(npm test)#1", 3]]
        assert rows[2:] == [""] * 3


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_finished_one_line_tool_unchanged():
    head = '⏺ context7 - Query Docs (MCP)(libraryId: "/x")'
    heads, rows = _fold(["", head, "  ⎿ \xa0Found 3 docs"])
    assert heads == [[1, head[2:] + "#1", 1]]
    assert rows[1] == head


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_identical_calls_keep_their_keys_across_the_blink():
    done = ["⏺ ctx - q (MCP)(a)", "  ⎿ \xa0done"]
    for beat in ("⏺ ctx - q (MCP)(a)", "  ctx - q (MCP)(a)"):
        heads, _ = _fold([""] + done + ["", beat, "  ⎿ \xa0Running…", ""] + done)
        assert [k for _, k, _ in heads] == ["ctx - q (MCP)(a)#1", "ctx - q (MCP)(a)#2", "ctx - q (MCP)(a)#3"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("rows", [
    # prose continuation with a call in it: no ⎿ under it
    ["", "⏺ You can call", "  someFunction(arg) to do X and", "  then more text."],
    # a prompt's continuation row, with a hook result under the prompt
    ["", "❯ please run the thing and", "  then tell me", "  ⎿ \xa0UserPromptSubmit hook said hi"],
])
def test_non_header_rows_are_not_given_a_bullet(rows):
    heads, out = _fold(rows)
    assert heads == []
    assert out == rows
