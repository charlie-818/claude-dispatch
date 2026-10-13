"""The phone's box-table pipeline (static/index.html) keeps rules and cell rows
on one grid.

Runs the real findTables/mendRule/compactBox/reflowBox/colorizeTable from
index.html in node on rows captured from a live pane, and checks that every
row of a table comes out the same length with its border glyphs in the same
columns, and that every box glyph is pinned to one text cell (.bx) — the
phone's fallback font draws ─ wider than a text cell otherwise.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "static" / "index.html").read_text()

# captured from the ~/tokenized-staking pane (iTerm2, 214 cols); the second
# table carries the U+FFFD pair iTerm2 sometimes returns for one ─
TABLES = [
    ["  ┌────────────┬──────────────┬───────────┬─────────────┬───────────┐",
     "  │   Width    │ Median $/day │ p25 $/day │ 7-day total │ Recentres │",
     "  ├────────────┼──────────────┼───────────┼─────────────┼───────────┤",
     "  │ ±10% (now) │ 15.2         │ 11.0      │ $108        │ 0         │",
     "  ├────────────┼──────────────┼───────────┼─────────────┼───────────┤",
     "  │ ±4%        │ 26.1         │ 23.4      │ $218        │ 1         │",
     "  ├────────────┼──────────────┼───────────┼─────────────┼───────────┤",
     "  │ ±3%        │ 33.2         │ 28.7      │ $272        │ 1         │",
     "  └────────────┴──────────────┴───────────┴─────────────┴───────────┘"],
    ["  ┌────────────────────┬──────┬──────────────┬──────┐",
     "  │        NFT         │ Fees │ Value change │ P&L  │",
     "  ├────────────────────┼──────┼──────────────┼──────┤",
     "  │ SPCX #1381099 ±10% │ +$63 │ −$83         │ −$20 │",
     "  ├─────��──────────────┼──────┼──────────────┼──────┤",
     "  │ NVDA #1381116 ±3%  │ +$18 │ −$78         │ −$60 │",
     "  └────────────────────┴──────┴──────────────┴──────┘"],
]


def _stretch(rows, extra=40):
    """Claude's last column stretched to fill a wide pane."""
    out = []
    for r in rows:
        fill = "─" if r.strip()[0] in "┌├└" else " "
        out.append(r[:-1] + fill * extra + r[-1])
    return out


def _js():
    def grab(start, end):
        i = HTML.index(start)
        return HTML[i:HTML.index(end, i)]
    return "\n".join([
        grab("const esc =", "\n") + "\n",
        grab("const URL_RE", "/* Shimmering"),
        grab("function colorizeWords", "\nfunction lineLayoutClass"),
        grab("const BOX_ROW", "// Scale each table block's font"),
    ])


def _run(tables, max_cols):
    script = _js() + """
const out = [];
for (const t of %s){
  const lines = t.slice();
  const r = findTables(lines, %d);
  out.push({lines, kind: r.kind, block: r.block, html: lines.map(colorizeTable)});
}
console.log(JSON.stringify(out));
""" % (json.dumps(tables), max_cols)
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


BOX = re.compile(r"[│┃║┌┐└┘├┤┬┴┼┏┓┗┛┣┫┳┻╋╭╮╰╯╔╗╚╝╞╡╤╧╪╟╢╒╕╘╛]")


def _assert_grid(lines):
    assert all("�" not in l for l in lines)
    assert len({len(l) for l in lines}) == 1, lines
    cols = [{m.start() for m in BOX.finditer(l)} for l in lines]
    # every row's border glyphs sit at the first row's junction columns
    assert all(c == cols[0] for c in cols), lines


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("max_cols", [0, 40])
def test_rules_and_cells_share_one_grid(max_cols):
    tables = TABLES + [_stretch(t) for t in TABLES]
    for t, src, got in zip(tables, TABLES * 2, _run(tables, max_cols)):
        assert all(got["kind"]) and len(set(got["block"])) == 1
        _assert_grid(got["lines"])
        if not max_cols:      # compacted: the stretched copy loses its empty band
            assert got["lines"][0] == src[0]
        for l, h in zip(got["lines"], got["html"]):
            assert h.count('class="bx"') == len(BOX.findall(l)) + l.count("─")


def test_box_glyphs_pinned_to_one_cell():
    rule = re.search(r"\.pane \.ln\.tbl \.bx\{([^}]*)\}", HTML)
    assert rule and "width:1ch" in rule.group(1)
