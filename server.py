#!/usr/bin/env python3
"""CC Dispatch — phone control for a fleet of live agent panes.

Reads and drives EXISTING iTerm2 sessions via the iTerm2 Python API. Nothing is
restarted. The one exception to "no session is created" is /api/spawn, which opens
a new Claude pane in a throwaway scratch dir on explicit request from the UI.

Claude Code, Codex and Grok are all tracked, off the same fleet records CC-Dash
reads (~/.claude/statusline.sh for Claude, ~/.claude/cc-active.sh for the other
two) plus each client's own session journal for its prompt/edit counters.

Safety model
------------
* Writes only ever happen in response to an authenticated request from the UI.
* Identity gates every write: a pane must be running one of those agents to
  receive keys, so scratch shells and unrelated panes are unreachable.
* Killing this process leaves the fleet exactly as it was.

Run:  .venv/bin/python server.py
"""
import asyncio
import errno
import glob
import json
import math
import os
import pathlib
import platform
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback

import iterm2
from aiohttp import web

import auth
import vault

HERE = pathlib.Path(__file__).parent
PORT = int(os.environ.get("DISPATCH_PORT", 8788))
# Loopback by default, on purpose. Reachability is Tailscale's job: `tailscale
# serve` terminates TLS and proxies to this port, so there is no listener on any
# network interface for a stranger to find. Binding anywhere else is opt-in and
# shouted about at startup.
BIND = os.environ.get("DISPATCH_BIND", "127.0.0.1")
FLEET_DIR = os.environ.get("CC_FLEET_DIR", "/tmp/cc-status")
POLL = 0.45                      # seconds between screen samples
STALE = 90                       # Claude fleet json older than this is dropped
# Codex and Grok write their record once per lifecycle event, not per render, so a
# quiet session's file goes cold while the pane is very much alive. Same retention
# CC-Dash gives them; the live pane list is what actually retires a row.
STALE_GENERIC = 4 * 3600
STALE_ENDED = 15 * 60            # keep a finished row long enough to review it
_FLEET_BY_TTY = {}               # /dev/ttysNNN -> record, for panes with no session id
_STARTED = time.time()           # process start, for /api/sysinfo uptime

# The interactive agent CLIs this fleet tracks. All three write the same shape of
# fleet record into FLEET_DIR — Claude via ~/.claude/statusline.sh, Codex and Grok
# via ~/.claude/cc-active.sh — so tracking one is tracking all three, on exactly
# the records CC-Dash already owns and prunes.
PROVIDERS = ("claude", "codex", "grok")
DEFAULT_PROVIDER = "claude"

# The project-instructions file each client reads on startup. Codex and Grok both
# read AGENTS.md; Claude reads CLAUDE.md. Only ever written into a scratch dir.
AGENT_NOTE_FILE = {"claude": "CLAUDE.md", "codex": "AGENTS.md", "grok": "AGENTS.md"}

# A pane may receive keystrokes if its foreground job is an agent CLI itself, OR if
# a fleet file was recently written for it. The second clause matters: while an
# agent runs a Bash tool the foreground job is that child process
# (bash/git/npm...), and gating on jobName alone would refuse Esc and Ctrl-C at
# precisely the moment you need them.
CHILD_JOBS = {"node", "caffeinate"}      # an agent's child, not the agent itself

# Panes confirmed to be an agent at any point, and which one. Identity does not
# expire: a pane sitting at a permission prompt stops re-rendering its statusline,
# so its fleet file ages out of the STALE window — and that is precisely when you
# need to answer it. Gating on freshness locked out exactly the wrong case.
KNOWN_AGENTS = {}                        # uuid -> provider

# Markers unique to each TUI's chrome, the last-resort identity check when jobName
# is a child process and no fleet file has landed yet. Every marker must be one
# that ONLY that client draws — a shared phrase would mislabel the pane.
# Only chrome the client DRAWS itself, never a phrase that could be discussed on
# screen: a Claude pane talking about Codex must not be read as a Codex pane.
MARKERS = {
    "claude": ("shift+tab to cycle", "for shortcuts", "bypass permissions on",
               "auto mode on", "plan mode on", "accept edits on",
               "manual mode on", "esc to interrupt"),
    "codex":  ("ask codex to do anything",),
    "grok":   ("· always-approve", "· auto-approve", "· approve-edits"),
}


def marker_provider(text):
    """Which client's TUI chrome is on this screen — None if it is nobody's."""
    low = (text or "").lower()
    for p in PROVIDERS:
        if any(m in low for m in MARKERS[p]):
            return p
    return None


def job_provider(job):
    """The client a foreground job IS. Matched on the leading word, because the
    binaries carry their platform and version in the name ("grok-1.0.5-macos",
    "claude.exe") and only the family part is stable."""
    j = (job or "").lower()
    for p in PROVIDERS:
        if j == p or j.startswith(p + "-") or j.startswith(p + "."):
            return p
    return None


def pane_provider(uuid, job, text=None):
    """Which agent CLI owns this pane — None for a plain shell.

    Record first, then screen, because a client's own fleet record is the only
    source that names the provider outright. jobName is checked before both when
    it IS the client binary, and the child-job fallback stays uncached so a Codex
    pane running `node` can still be identified properly a poll later.
    """
    u = (uuid or "").upper()
    known = KNOWN_AGENTS.get(u)
    if known:
        return known
    p = job_provider(job)
    if not p:
        p = (read_fleet_files().get(u) or {}).get("provider") or marker_provider(text)
    if p:
        KNOWN_AGENTS[u] = p
        return p
    return DEFAULT_PROVIDER if job in CHILD_JOBS else None


def is_agent_pane(uuid, job, text=None):
    return pane_provider(uuid, job, text) is not None


def provider_of(uuid):
    """The provider we have already established for a pane."""
    return KNOWN_AGENTS.get((uuid or "").upper()) or DEFAULT_PROVIDER


def claude_only(uuid, what):
    """Guard for the controls that read or drive Claude's own TUI chrome.

    Permission mode, effort and model are steered by watching Claude's status
    line and pressing keys until it changes. Codex and Grok draw nothing of the
    sort, so the loop would type into a pane that can never satisfy it — refuse
    with a reason instead.
    """
    p = provider_of(uuid)
    if p == DEFAULT_PROVIDER:
        return None
    return web.json_response(
        {"error": f"{what} is a Claude control — this pane is running {p}"},
        status=409)

TOKEN_FILE = HERE / ".token"
if TOKEN_FILE.exists():
    TOKEN = TOKEN_FILE.read_text().strip()
else:
    TOKEN = secrets.token_urlsafe(18)
    TOKEN_FILE.write_text(TOKEN)
    TOKEN_FILE.chmod(0o600)

# ── key map ────────────────────────────────────────────────────────────────
# Every value is written in ONE send_text call so terminals parse it as a
# single key, never as loose bytes. See probe_keys.py for the validation.
KEYS = {
    "esc":   "\x1b",
    "^C":    "\x03",
    "^D":    "\x04",
    "s-tab": "\x1b[Z",
    "tab":   "\t",
    "up":    "\x1b[A",
    "down":  "\x1b[B",
    "left":  "\x1b[D",
    "right": "\x1b[C",
    "enter": "\r",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5",
    "6": "6", "7": "7", "8": "8", "9": "9",
    "space": " ",
}

CONN = None          # iterm2.Connection
APP = None           # iterm2.App

# ── Grid layout ─────────────────────────────────────────────────────────────
# Spawns become splits in the fleet's own tab, arranged into a grid. Growth is
# ROW-MAJOR so the dividers stay aligned: fill the top row across up to MAX_COLS
# full-height columns, THEN drop a second row into each column, and so on. Row-
# major is the only single-split growth path that keeps a clean NxM grid — a
# column-first order leaves later columns split inside one pane's region and the
# dividers no longer line up.
GRID_MAX_COLS = 3    # top row grows to this many columns before rows start filling

# Canonical Claude-pane width. The phone renderer sizes its font so `cols`
# characters fill the screen, capped at 15px — so a pane narrower than this
# blows the font past the cap and shows big text in only half the width (the
# "zoomed" look on a host whose iTerm font is large / window narrow). Widen any
# pane below this to the target so every host renders at the same column count.
# GROW ONLY, never shrink: a pane already at/above target belongs to a window
# the user may be physically looking at, and yanking it smaller is hostile.
# Rows matter as much as cols — a short pane (Big Mac's fleet window was only
# ~456px tall → 11 rows) crushes Claude's TUI vertically, cutting off the top and
# never reaching the bottom, even when the width already matches.
PANE_COLS = 52
# Rows are the pane floor for every client. A dozen tiled panes cannot each be
# phone-height in one window — iTerm refuses the resize.
PANE_ROWS = 25
COL_TOL = 3              # accept 52..55 cols (a tiled window fills to 53/54); only
                        # a pane outside this band (e.g. font-drift balloon) is reset


# ── iTerm helpers ──────────────────────────────────────────────────────────
async def all_sessions():
    """Every live pane, keyed by uppercase session UUID.

    `tab.all_sessions`, not `tab.sessions`: when one pane is maximized (a phone
    watching it via grow_pane, or a hand on the Mac hitting ⇧⌘⏎) iTerm reports
    its tab-mates as *minimized* and drops them from `sessions`. They are still
    alive and still readable — screen, scrollback, line info all answer — only
    their grid_size comes back None. Skipping them told the phone "pane closed"
    about a chat that was merely hidden.
    """
    await APP.async_refresh()
    out = {}
    for w in APP.terminal_windows:
        for t in w.tabs:
            for s in t.all_sessions:
                out[s.session_id.upper()] = s
    return out


def _column_sessions(node):
    """Leaf sessions under a column node, top-to-bottom."""
    if isinstance(node, iterm2.Session):
        return [node]
    out = []
    for c in node.children:
        out.extend(_column_sessions(c))
    return out


def grid_columns(tab):
    """The tab's panes grouped into visual columns, left-to-right.

    Returns a list of columns; each column is a list of Sessions top-to-bottom.
    A vertical splitter at the root means its children ARE the columns; a
    horizontal root (or a bare session) is a single column.
    """
    root = tab.root
    if isinstance(root, iterm2.Session):
        return [[root]]
    if root.vertical:                      # dividers vertical -> children side by side
        return [_column_sessions(child) for child in root.children]
    return [_column_sessions(root)]        # dividers horizontal -> one stacked column


def pick_grid_split(tab):
    """Where the next pane should go to grow the grid row-major.

    Returns (session_to_split, vertical) — vertical=True makes a new column to
    the right, vertical=False drops a new row below the chosen pane.
    """
    cols = grid_columns(tab)
    building_top_row = all(len(c) == 1 for c in cols)
    if building_top_row and len(cols) < GRID_MAX_COLS:
        # New full-height column on the right (every column is one full-height pane).
        return cols[-1][0], True
    # Fill a row: the leftmost column with the fewest rows, split its bottom pane.
    target = min(range(len(cols)), key=lambda i: (len(cols[i]), i))
    return cols[target][-1], False


def fleet_tab(app):
    """The tab holding the most known-Claude panes, across ALL windows.

    Scanning every window matters: a spawn triggered from the phone runs while
    iTerm is unfocused, so `current_terminal_window` is None and the fleet may
    not live in `terminal_windows[0]` — splitting there would open the pane in
    the wrong window. Falls back to the current/first window's current tab.
    """
    best, best_n = None, 0
    for w in app.terminal_windows:
        for t in w.tabs:
            n = sum(1 for s in t.all_sessions if s.session_id.upper() in KNOWN_AGENTS)
            if n > best_n:
                best, best_n = t, n
    if best is not None:
        return best
    win = app.current_terminal_window or (
        app.terminal_windows[0] if app.terminal_windows else None)
    return win.current_tab if win else None


def trust_dir(path):
    """Pre-mark a directory trusted in ~/.claude.json so the first-run trust
    dialog never appears for it.

    Claude reads `projects[<abspath>].hasTrustDialogAccepted` at startup. We only
    ADD our fresh scratch path's entry — never touch other projects — so a
    concurrent Claude rewriting the file can at worst drop this one new key (the
    dialog reappears once), never corrupt anything else.
    """
    import json
    import os
    p = os.path.expanduser("~/.claude.json")
    try:
        with open(p) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    key = os.path.abspath(path)
    projects = cfg.setdefault("projects", {})
    entry = projects.setdefault(key, {})
    entry["hasTrustDialogAccepted"] = True
    entry.setdefault("hasCompletedProjectOnboarding", True)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, p)                 # atomic swap; no half-written config
    except Exception as e:
        print(f"  [trust] could not pre-trust {key}: {type(e).__name__}: {e}",
              flush=True)


# Claude's live status footer — the spinner line ("· Seasoning… (2m 52s)") and
# its elapsed timer — re-renders every tick. On a narrow split pane it hard-wraps
# and each tick scrolls a fresh copy into scrollback, so `pane_history` ships the
# same rotating-tip / spinner block stacked dozens of times. Strip the ever-
# changing spinner line (so the surrounding footer copies become byte-identical),
# then collapse adjacent duplicate blocks. Applied to scrollback ONLY — the live
# screen (`pane_text`) keeps its footer so the phone still shows real-time status.
_SPINNER_RE = re.compile(r"…\s*\((?:\d+m(?:\s*\d+s)?|\d+s)\)\s*$")


def _is_spinner(line):
    s = line.strip().lstrip("·⎿✢✳✶✽*⏺ ").strip()
    return bool(s) and bool(_SPINNER_RE.search(s))


def _collapse_repeats(lines, max_block=16):
    """Drop runs of adjacent identical blocks, keeping one copy. GATED to the
    leaked status footer: only a block that contains the rotating Tip banner is
    ever collapsed, so real chat content — double blanks, repeated box borders,
    two identical code lines — is preserved byte-for-byte and the phone view
    stays character-identical to the terminal (and to any other host)."""
    out = lines
    for blk in range(min(max_block, len(out) // 2), 0, -1):
        res, i, n = [], 0, len(out)
        while i < n:
            block = out[i:i + blk]
            if (i + 2 * blk <= n and block == out[i + blk:i + 2 * blk]
                    and any("Tip:" in x for x in block)):
                res.extend(block)                   # keep first copy
                j = i + blk
                while j + blk <= n and out[j:j + blk] == block:
                    j += blk                        # skip every further repeat
                i = j
            else:
                res.append(out[i])
                i += 1
        out = res
    return out


def clean_history(lines):
    # Only the ephemeral status footer is touched: the spinner/elapsed line is
    # dropped, and repeated Tip-banner blocks are collapsed to one. Everything
    # else is untouched, so scroll length, wrap width and formatting match the
    # source terminal exactly on every host.
    kept = [l for l in lines if not _is_spinner(l)]
    return _collapse_repeats(kept)


async def pane_history(session, max_lines=600, since=None):
    """Scrollback above the visible screen, so the phone can read the whole chat.

    Absolute line numbers run from `overflow` (oldest line iTerm still holds)
    upward; anything below that has already been discarded. Returns
    (lines, end) where `end` is the absolute number of the first screen row —
    pass it back as `since` to get only what scrolled off the screen after it.

    Streaming the growth matters for the phone's scroll position: the live
    screen is a 25-row window that iTerm shifts up as output arrives. If the
    phone only ever held the scrollback it got at open, every shift rewrote all
    25 live rows in place, and in wrap mode their heights changed under the
    reader's finger — Safari has no scroll anchoring, so the view jumped.
    Appending the rows that scrolled off makes the DOM append-only: the row that
    left the screen lands in history with the same text at the same index.
    """
    try:
        info = await session.async_get_line_info()
        start = info.overflow
        end = info.overflow + info.scrollback_buffer_height
        if since is not None:
            if end <= since:
                return [], since
            start = max(start, since)
            if end - start > 2000:      # a huge burst — keep the recent tail
                start = end - 2000
        else:
            start = max(start, end - max_lines)
        if end <= start:
            return [], end
        lines = await session.async_get_contents(start, end - start)
        return clean_history(
            [l.string.replace("\x00", " ").rstrip() for l in lines]), end
    except Exception as e:
        print(f"  [history] failed: {type(e).__name__}: {e}", flush=True)
        return [], since


_COLS_SEEN = {}   # uuid -> last width read while the pane had a grid


async def pane_cols(session):
    """Column count. A minimized pane (tab-mate of a maximized one) has no grid,
    so serve the width it had when last visible rather than a guess — the phone
    sizes its font to this, and a wrong number reflows the whole transcript."""
    try:
        w = int(session.grid_size.width)
        _COLS_SEEN[session.session_id.upper()] = w
        return w
    except Exception:
        return _COLS_SEEN.get(session.session_id.upper(), 80)


CANON_FONT = "Monaco 12"   # reference font; matches MacBook so 52 cols fills a
                           # pane's width the same way. Font is coupled to BOTH
                           # cols and rows (same size scales each), so a per-host
                           # font drift changes the column count — keep it uniform.


async def _pane_font(session):
    try:
        return (await session.async_get_profile()).normal_font
    except Exception:
        return CANON_FONT


async def _set_font(session, spec):
    try:
        chg = iterm2.LocalWriteOnlyProfile()
        chg.set_normal_font(spec)
        await session.async_set_profile_properties(chg)
        return True
    except Exception as e:
        print(f"  [normalize] font set failed: {type(e).__name__}: {e}", flush=True)
        return False


async def normalize_pane(session):
    """Single nudge for a freshly spawned pane: canonical font, cols pinned to
    PANE_COLS, rows grown toward PANE_ROWS. Full convergence is normalize_all."""
    try:
        if await _pane_font(session) != CANON_FONT:
            await _set_font(session, CANON_FONT)
        g = session.grid_size
        if g is None:                      # minimized behind a maximized tab-mate
            return False
        w, h = int(g.width), int(g.height)
        nw = w if PANE_COLS <= w <= PANE_COLS + COL_TOL else PANE_COLS
        nh = max(h, PANE_ROWS)
        if (nw, nh) != (w, h):
            await session.async_set_grid_size(iterm2.util.Size(nw, nh))
        return True
    except Exception as e:
        print(f"  [normalize] {type(e).__name__}: {e}", flush=True)
        return False


async def normalize_all(passes=10):
    """Normalise every known Claude pane toward the reference geometry so the phone
    view matches across hosts. Runs at startup / after a SIGHUP reload so an
    existing off-size host (Big Mac) converges without a respawn.

    Per pane, each sweep:
      • restore the canonical font — cols and rows scale with font size, so any
        drift (e.g. an earlier shrink) throws the column count off;
      • pin cols to exactly PANE_COLS and grow rows toward PANE_ROWS.

    Width (cols) is the parity that matters most — it fixes the "zoomed / half
    screen" look and makes line-wrapping identical — so it is pinned exactly.
    Rows are grown as far as the window allows: on a narrower screen a tiled pane
    is physically shorter than the reference and cannot reach PANE_ROWS without
    inflating cols (font is shared between the two axes), so height is best-effort,
    not forced. Re-sweeping converges the tiled squeeze; stop when a sweep is a
    no-op."""
    changes = 0
    for _ in range(passes):
        try:
            sessions = await all_sessions()    # refreshes APP, so grid_size is live
        except Exception as e:
            print(f"  [normalize] scan failed: {type(e).__name__}: {e}", flush=True)
            return
        acted = False
        for uuid in list(KNOWN_AGENTS):
            s = sessions.get(uuid)
            if s is None:
                continue
            if await _pane_font(s) != CANON_FONT:
                await _set_font(s, CANON_FONT)
                acted = True
                changes += 1
            g = s.grid_size
            if g is None:                  # minimized behind a maximized tab-mate
                continue
            w, h = int(g.width), int(g.height)
            nw = w if PANE_COLS <= w <= PANE_COLS + COL_TOL else PANE_COLS
            nh = max(h, PANE_ROWS)
            if (nw, nh) != (w, h):
                try:
                    await s.async_set_grid_size(iterm2.util.Size(nw, nh))
                    acted = True
                    changes += 1
                except Exception as e:
                    print(f"  [normalize] {type(e).__name__}: {e}", flush=True)
        if not acted:
            break
        await asyncio.sleep(0.4)           # let iTerm settle the reflow before re-measuring
    if changes:
        print(f"  [normalize] cols={PANE_COLS}, rows>={PANE_ROWS} best-effort "
              f"({changes} change(s))", flush=True)


async def pane_text(session):
    c = await session.async_get_screen_contents()
    # iTerm returns NUL for every unwritten cell, not space. NULs are stripped
    # by innerHTML, which collapses the layout — translate them back to spaces.
    return "\n".join(c.line(i).string.replace("\x00", " ").rstrip()
                     for i in range(c.number_of_lines)).rstrip()


OPTION_RE = re.compile(r"^\s*[❯>›]?\s*(\d+)\.\s+(\S.*?)\s*$")
# The caret Claude or Codex parks on the highlighted row of a select dialog.
# Its presence separates a live prompt from a merely printed numbered list.
SELECT_RE = re.compile(r"^\s*[❯>›]\s")

# A leading checkbox glyph on an option label — multi-select (AskUserQuestion
# with multiSelect) draws one of these instead of a number. group(1) is the
# box itself, group(2) the label with the box stripped off.
CHECK_RE = re.compile(r"^([☐☑☒◻◼◽◾○●⬡⬢]|\[[ xX✓·]?\])\s*(.*)$")
_CHECKED_MARKS = set("☑☒◼◾⬢●")
# Claude prints this hint under a checkbox run even when a row happens to
# have no box drawn (a header row, say) — the fallback signal for "multi".
MULTI_HINT_RE = re.compile(r"space to (toggle|select)", re.I)


def _check_state(mark):
    if mark in _CHECKED_MARKS:
        return True
    if mark.startswith("["):
        return mark[1:-1] in ("x", "X", "✓", "·")
    return False


# The input box is fenced by a horizontal rule above and below; long input
# hard-wraps onto the rows in between, which carry no caret of their own.
BOX_RULE_RE = re.compile(r"^[─━╌╍—_-]{4,}$")

# AskUserQuestion's chrome: a tab per question above ("☐ Colour", ticked once
# answered) and a key legend under the rows.
ASK_TAB_RE = re.compile(r"^\s*[☐☑☒]\s+\S")
ASK_FOOT_RE = re.compile(r"Enter to select", re.I)


def read_input_box(text):
    """(ghost, text) for Claude's ❯ box — "" when it is empty.

    Reading only the caret row silently truncated anything wider than the pane
    (39 columns on a phone-sized session), both for the composer hint and for
    the retype-and-submit path. So walk DOWN from the caret to the box's closing
    rule and rejoin the wrapped rows.

    A row the terminal filled to the edge was split mid-token and is rejoined
    with no space; a shorter row ended on a word boundary and gets its space
    back. Without a closing rule we are not looking at the box at all (most
    likely the ❯ caret of a select dialog), so only the caret row is taken.
    """
    lines = text.splitlines()
    for n in range(len(lines) - 1, -1, -1):
        i = lines[n].find("❯")
        if i == -1:
            continue
        rest = lines[n][i + 1:]
        ghost = rest[:1] == "\xa0"                 # ❯\xa0… = suggestion, ❯ … = typed
        out = rest.replace("\xa0", " ").strip(" │╎")
        # Collect the wrapped rows, but only as far as a closing rule: a blank
        # line or a numbered option means this caret was never an input box.
        cont, width = [], None
        for l in lines[n + 1:]:
            body = l.replace("\xa0", " ").strip(" │╎")
            if BOX_RULE_RE.match(body):
                width = len(l)                     # the rule spans the box
                break
            if not body or OPTION_RE.match(l):
                break
            cont.append(l)
        if width:
            prev = lines[n]
            for l in cont:
                # >= width - 1 leaves a column of slack for a wide glyph that
                # could not fit in the last cell.
                out += ("" if len(prev) >= width - 1 else " ")
                out += l.replace("\xa0", " ").strip(" │╎")
                prev = l
        return ghost, out.strip()
    return False, ""


def detect_input(text, provider=None):
    """The text sitting in Claude's ❯ input box — a greyed ghost suggestion
    (rendered after a NON-breaking space) or already-typed/queued text. We strip
    the box from the phone's pane view, so surface this so it still shows in the
    mobile composer. Returns {"text","ghost"} or None."""
    if provider == "codex":
        if _codex_prompt(text):
            return None
        ghost, s = _codex_input(text)
    elif provider == "grok":
        if _grok_prompt(text):
            return None
        ghost, s = read_input_box(text)
    else:
        ghost, s = read_input_box(text)
    if not s:
        return None
    return {"text": s[:600], "ghost": ghost}


def _fold_tail(cur, l, indent, label_col):
    """Classify a line that isn't itself an option/checkbox row: fold it into
    the option above if it's a hard-wrapped continuation (a narrow pane — a
    split can be 16 columns wide — wraps every option onto lines indented to
    the label column), swallow a blank gutter, or signal the run should close.
    Shared by the numbered and checkbox run-builders below.
    """
    if not cur:
        return "skip"
    s = l.strip()
    if s and indent >= label_col and not set(s) <= set("─━│ ⎿"):
        # kept apart from the label: a permission prompt's wrapped label gets
        # rejoined at emit time, AskUserQuestion's description stays a description
        cur[-1][4] = (cur[-1][4] + " " + s).strip()[:200]
        return "fold"
    if not s:
        return "blank"                 # blank gutter between options is fine
    # AskUserQuestion rules off its trailing "Chat about this" row from the
    # answers above it — a rule inside a run is a divider, not the end
    if BOX_RULE_RE.match(s):
        return "blank"
    return "close"


def _last_numbered_run(lines):
    """Last run of consecutive numbered option lines with the caret parked on
    exactly one of them — a numbered list Claude simply wrote out (the tail of
    a plan, a summary ending in bullets) carries no caret and doesn't count."""
    runs, cur = [], []
    label_col = 0

    def close():
        if cur:
            runs.append(list(cur))
            cur.clear()

    for i, l in enumerate(lines):
        m = OPTION_RE.match(l)
        indent = len(l) - len(l.lstrip())
        if m:
            if cur and int(m.group(1)) != len(cur) + 1:
                close()                # a number out of sequence starts a new run
            if not cur:
                label_col = l.index(m.group(2))
            cur.append([i, m.group(1), m.group(2), bool(SELECT_RE.match(l)), ""])
            continue
        if _fold_tail(cur, l, indent, label_col) == "close":
            close()
    close()
    runs = [r for r in runs if len(r) >= 2 and sum(1 for o in r if o[3]) == 1]
    return runs[-1] if runs else None


def _last_unnumbered_run(lines):
    """A multi-select rendered with no numbers at all — just a caret and a
    checkbox glyph per row, e.g. "  ❯ ☑ Option A" / "    ☐ Option B". Only
    tried when the numbered parser above finds nothing, since Claude numbers
    the vast majority of dialogs.
    """
    runs, cur = [], []
    label_col = 0

    def close():
        if cur:
            runs.append(list(cur))
            cur.clear()

    for i, l in enumerate(lines):
        indent = len(l) - len(l.lstrip())
        s = l.lstrip()
        caret = s[:1] in ("❯", ">")
        m = CHECK_RE.match(s[1:].lstrip() if caret else s)
        if m:
            if not cur:
                label_col = indent
            cur.append([i, m.group(1), m.group(2), caret, ""])
            continue
        if _fold_tail(cur, l, indent, label_col) == "close":
            close()
    close()
    runs = [r for r in runs if len(r) >= 2 and sum(1 for o in r if o[3]) == 1]
    return runs[-1] if runs else None


def _is_option_row(l):
    """True for anything the question-text walk-up should treat as the start
    of the option list, numbered or checkbox — used to stop the upward scan."""
    if OPTION_RE.match(l):
        return True
    s = l.lstrip()
    if s[:1] in ("❯", ">"):
        s = s[1:].lstrip()
    return bool(CHECK_RE.match(s))


def detect_prompt(text, provider=None, uuid=None):
    """Find a choice Claude is waiting on: permission/plan approval (numbered,
    single choice) or a checkbox multi-select (AskUserQuestion multiSelect,
    numbered or not). Returns {"question", "options", "multi"} or None, where
    each option is {"key","label","selected","checked","index"}. Only the LAST
    run of consecutive option lines counts — earlier ones are scrollback from
    prompts already answered.
    """
    if provider == "codex":
        prompt = _codex_prompt(text)
        if uuid:
            pane = uuid.upper()
            if prompt is None:
                _CODEX_PROMPT_EPISODES.pop(pane, None)
            else:
                identities = _CODEX_PROMPT_EPISODES.setdefault(pane, {})
                immutable = json.dumps([prompt["kind"], prompt.get("variant"), prompt["question"], prompt.get("context"),
                                        prompt.get("question_index"), prompt.get("question_count"),
                                        [(o["label"], o.get("desc", "")) for o in prompt["options"]]])
                prompt["id"] = identities.setdefault(immutable, secrets.token_hex(16))
        return prompt
    if provider == "grok":
        prompt = _grok_prompt(text)
        if uuid:
            pane = uuid.upper()
            if prompt is None:
                _GROK_PROMPT_EPISODES.pop(pane, None)
            else:
                identities = _GROK_PROMPT_EPISODES.setdefault(pane, {})
                immutable = json.dumps([prompt["kind"], prompt["question"],
                                        [(o["label"], o.get("desc", "")) for o in prompt["options"]]])
                prompt["id"] = identities.setdefault(immutable, secrets.token_hex(16))
        return prompt
    lines = text.splitlines()
    run = _last_numbered_run(lines)
    numbered = run is not None
    if run is None:
        run = _last_unnumbered_run(lines)
    if run is None:
        return None

    # The question sits above the first option, but the terminal may have
    # hard-wrapped it ("Would you like to / proceed?"), so walk upward and
    # rejoin the run of non-blank lines rather than taking only the last one.
    parts = []
    for j in range(run[0][0] - 1, max(-1, run[0][0] - 9), -1):
        cand = lines[j].strip()
        if not cand:
            if parts:                  # blank above the text block ends it
                break
            continue                   # blank between question and options
        if _is_option_row(lines[j]) or set(cand) <= set("─━│ ⎿"):
            break
        parts.append(cand)
    q = " ".join(reversed(parts))

    # AskUserQuestion draws a tab strip ("☐ Colour") above the question and an
    # "Enter to select" footer below the rows; its indented lines under each
    # option are descriptions. A permission prompt has neither, and its indented
    # lines are a hard-wrapped label — those get rejoined.
    head = lines[max(0, run[0][0] - 6):run[0][0]]
    tail = lines[run[-1][0] + 1:run[-1][0] + 5]
    ask = any(ASK_TAB_RE.match(l) for l in head) or any(ASK_FOOT_RE.search(l) for l in tail)

    opts, multi = [], False
    for idx, (_, k, lbl, sel, more) in enumerate(run):
        if numbered:
            m = CHECK_RE.match(lbl)
            box, label, key = (m.group(1), m.group(2), k) if m else (None, lbl, k)
        else:
            box, label, key = k, lbl, ""   # no number to press on this row
        checked = _check_state(box) if box is not None else False
        multi = multi or box is not None
        desc = ""
        if more:
            if ask:
                desc = more
            else:
                label = (label + " " + more)[:200]
        o = {"key": key, "label": label[:70], "selected": sel,
             "checked": checked, "index": idx}
        if desc:
            o["desc"] = desc[:120]
        opts.append(o)
    if not multi:
        for l in lines[run[-1][0] + 1: run[-1][0] + 5]:
            if MULTI_HINT_RE.search(l):
                multi = True
                break

    return {"question": q[:160], "options": opts[:9], "multi": multi}


_GROK_PROMPT_EPISODES = {}
# Grok's slash pickers sit between two dashed rules. The top rule stamps the
# visible row count just before the last dash ("──2─"); the bottom is plain.
_GROK_RULE = re.compile(r"^\s*[─━]{4,}\d*[─━]*\s*$")
_GROK_BAR_TAIL = re.compile(r" {2,}[█▌│]+\s*$")
_GROK_PICKER_CMD = re.compile(r"^/(model|effort)\b.+$")
_GROK_STATUS = re.compile(
    r"╰─+\s*(.*?)\s*(?:\((\w+)\))?\s*(?:·\s*([\w-]+))?\s*─*╯")
_GROK_MODE_MARK = {
    "always-approve": "always-approve",
    "auto-approve": "auto",
    "approve-edits": "accept",
    "plan": "plan",
}


def _grok_clean(line):
    return _GROK_BAR_TAIL.sub("", line or "").rstrip()


def detect_grok_status(text):
    """Model, effort and permission mode from Grok's input-box footer."""
    out = {}
    for raw in reversed((text or "").splitlines()):
        line = _grok_clean(raw)
        m = _GROK_STATUS.search(line)
        if not m:
            continue
        name = (m.group(1) or "").strip()
        if name:
            out["model"] = name
        if m.group(2):
            out["effort"] = m.group(2).lower()
        mark = (m.group(3) or "").lower()
        if mark in _GROK_MODE_MARK:
            out["mode"] = _GROK_MODE_MARK[mark]
        elif mark:
            out["mode"] = mark
        else:
            out["mode"] = "ask"
        return out
    return out


def _grok_picker_command(text):
    """The slash command currently driving a Grok dropdown, or None.

    `/model` or `/effort` alone is the autocomplete menu. The picker itself
    fills the box with a template (`/model <model> [effort]`, `/effort <level>`)
    or the chosen model (`/model Grok 4.6`) while effort rows are showing.
    """
    ghost, s = read_input_box(text)
    cmd = (s or "").strip()
    if ghost:
        return None
    return cmd.split()[0] if _GROK_PICKER_CMD.match(cmd) else None


def _grok_label_col(line):
    s = line.lstrip()
    if s.startswith("❯"):
        return line.find("❯") + 2
    return len(line) - len(s)


# Grok's TUI can paint a model logo (image cell / private-use glyph) before the
# name. Those are not part of the option and they break wrapping on a 52-col pane.
_GROK_ICON = re.compile(
    r"^[\s\uE000-\uF8FF￼�◆◇▶▷●○■□▪▫✦✧★☆⭐🖼🖥]+")


def _grok_strip_icon(text):
    return _GROK_ICON.sub("", text or "").strip()


def _grok_prompt(text):
    """Grok's `/model` and `/effort` dropdowns: unnumbered caret rows between
    two dashed rules, parked above the input box. Returns a picker dict or None.
    """
    command = _grok_picker_command(text)
    if not command:
        return None
    lines = [_grok_clean(l) for l in (text or "").splitlines()]
    rules = [i for i, l in enumerate(lines) if _GROK_RULE.match(l)]
    if len(rules) < 2:
        return None
    start, end = rules[-2], rules[-1]
    if end - start < 2:
        return None
    rows, label_col = [], None
    for line in lines[start + 1:end]:
        if not line.strip():
            continue
        col = _grok_label_col(line)
        if label_col is None:
            label_col = col
        if col > label_col and rows:
            extra = line.strip()
            if extra:
                rows[-1]["rest"] = (rows[-1]["rest"] + " " + extra).strip()
            continue
        if col < label_col:
            continue
        rest = line[col:].rstrip()
        if not rest.strip() or rest.lstrip().startswith("/"):
            continue
        rows.append({"selected": "❯" in line[:col], "rest": rest.strip()})
    desc_col = None
    for rest in (r["rest"] for r in rows):
        m = re.search(r"\s{2,}\S", rest)
        if m:
            at = m.end() - 1
            desc_col = at if desc_col is None else min(desc_col, at)
    options = []
    for row in rows:
        rest = row["rest"]
        label, desc = rest, ""
        if desc_col is not None and len(rest) > desc_col:
            label, desc = rest[:desc_col].rstrip(), rest[desc_col:].strip()
        else:
            marked = re.match(r"^(.+? \((?:current|active)\))\s+(\S.*)$", rest)
            if marked:
                label, desc = marked.group(1), marked.group(2)
        option = {"key": "", "label": _grok_strip_icon(label), "selected": row["selected"],
                  "checked": False, "index": len(options)}
        if desc:
            option["desc"] = desc
        options.append(option)
    if len(options) < 2 or sum(1 for o in options if o["selected"]) != 1:
        return None
    kind = "effort" if command == "/effort" or all(
        "effort" in o["label"].lower() for o in options) else "model"
    question = "Select reasoning effort" if kind == "effort" else "Select model"
    return {"provider": "grok", "kind": "picker", "variant": kind,
            "question": question, "context": "", "options": options,
            "multi": False, "actions": ["choose", "cancel"]}


_CODEX_PROMPT_EPISODES = {}
_CODEX_QUESTION = re.compile(r"^\s*Question (\d+)/(\d+)(?:\s+\([^)]*\))?\s*$")
_CODEX_PLACEHOLDERS = {"Ask Codex to do anything", "Add notes", "Add notes or text", "Type your answer (optional)",
                       "Write tests for @filename", "Explain this codebase", "Find and fix a bug in @filename",
                       "Implement {feature}", "Summarize recent commits", "Improve documentation in @filename"}


_CODEX_INPUT_FOOT = re.compile(
    r"context left|for shortcuts|to submit|to clear notes|to add notes|esc to|gpt-\S+.*·", re.I)
_CODEX_FINISHED_TURN = re.compile(r"^[•■⚠]")
_CODEX_PICKER_TITLES = re.compile(
    r"^Select Model$|^Select Model and Effort$|^Advanced Reasoning$|^Select Reasoning Level\b|"
    r"^Apply reasoning change$|^Choose where to apply\b")


def _codex_input(text):
    animation = re.compile(r"(?=.*[\u2800-\u28ff])[\s\u2800-\u28ff]+", re.S)
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if not line.lstrip().startswith("›") or OPTION_RE.match(line):
            continue
        value = line.lstrip()[1:].lstrip()
        if animation.fullmatch(value):
            value = ""
        continuation, finished = [], False
        for tail in lines[i + 1:]:
            if animation.fullmatch(tail):
                continue
            stripped = tail.strip()
            if not stripped or _CODEX_INPUT_FOOT.search(stripped):
                break
            if OPTION_RE.match(tail) or BOX_RULE_RE.match(stripped):
                break
            if _CODEX_FINISHED_TURN.search(stripped):
                finished = True
                break
            continuation.append(tail[2:] if tail.startswith("  ") else tail)
        if finished:
            continue
        value = "\n".join([value, *continuation]).rstrip()
        for placeholder in sorted(_CODEX_PLACEHOLDERS, key=len, reverse=True):
            if value.startswith(placeholder):
                decoration = value[len(placeholder):]
                if decoration and decoration[0].isspace() and animation.fullmatch(decoration):
                    value = placeholder
                    break
        return value in _CODEX_PLACEHOLDERS, value
    return False, ""


def _codex_prompt(text):
    asynchronous = _codex_async_prompt(text)
    if asynchronous:
        return asynchronous
    lines = text.splitlines()
    headers = [(i, _CODEX_QUESTION.match(line)) for i, line in enumerate(lines)
               if _CODEX_QUESTION.match(line)]
    if headers:
        start, header = headers[-1]
        body = lines[start + 1:]
        footer = next((i for i, line in enumerate(body) if re.search(r"enter to submit (?:answer|all)", line, re.I)), None)
        if footer is not None:
            if any(line.lstrip().startswith("›") and not OPTION_RE.match(line) for line in body[footer + 1:]):
                return None
            content = body[:footer]
            # A completed transcript below a stale header is not an overlay.
            if not any(re.search(r"tab|esc to interrupt|navigate questions", line, re.I) for line in body[footer:footer + 3]):
                return None
            options = []
            first = next((i for i, line in enumerate(content) if OPTION_RE.match(line)
                          or line.lstrip().startswith("›")), len(content))
            question = "\n".join(line.strip() for line in content[:first]).strip()
            for line in content[first:]:
                match = OPTION_RE.match(line)
                if match:
                    parts = re.split(r"\s{2,}", match[2], maxsplit=1)
                    option = {"key": match[1], "label": parts[0], "selected": bool(SELECT_RE.match(line)),
                              "checked": False, "index": len(options)}
                    if len(parts) > 1:
                        option["desc"] = parts[1]
                    options.append(option)
                elif line.lstrip().startswith("›"):
                    break
                elif line.strip() and options:
                    options[-1]["desc"] = (options[-1].get("desc", "") + " " + line.strip()).strip()
            ghost, draft = _codex_input("\n".join(content))
            visible = any(line.lstrip().startswith("›") and not OPTION_RE.match(line) for line in content)
            index, count = int(header[1]), int(header[2])
            return {"provider": "codex", "kind": "question", "question": question,
                    "context": "", "options": options, "multi": False,
                    "question_index": index, "question_count": count,
                    "input": {"allowed": True, "visible": visible, "text": "" if ghost else draft,
                              "placeholder": draft if ghost else "Add notes or text"},
                    "actions": ["submit", *(["previous"] if index > 1 else []),
                                *(["next"] if index < count else []), "cancel"]}
    # Native approval/model dialogs have a confirmation footer. Plain numbered
    # prose, including old answered questions, has no such live footer.
    footers = [i for i, line in enumerate(lines) if re.search(r"(?:press )?enter to (?:confirm|select)", line, re.I)]
    if not footers:
        return None
    end = footers[-1]
    if any(line.lstrip().startswith("›") and not OPTION_RE.match(line) for line in lines[end + 1:]):
        return None
    run = _last_numbered_run(lines[:end])
    if not run:
        return None
    start = run[0][0]
    titles = [i for i, line in enumerate(lines[:start]) if re.search(
        r"Would you like to|Do you want to|^\s*(?:Select |Advanced Reasoning)|(?<!\bwithout your )approval|\bapprove\b",
        line, re.I)]
    head_start = titles[-1] if titles else max(0, start - 8)
    context = "\n".join(line.strip() for line in lines[head_start:start]).strip()
    approval = bool(re.search(
        r"Would you like to|Do you want to|run the following|(?<!\bwithout your )approval|\bapprove\b",
        context, re.I))
    options = []
    for i, (_, key, label, selected, tail) in enumerate(run):
        parts = re.split(r"\s{2,}", label, maxsplit=1)
        option = {"key": key, "label": parts[0], "selected": selected, "checked": False, "index": i}
        desc = " ".join(filter(None, [parts[1] if len(parts) > 1 else "", tail]))
        if desc:
            option["desc"] = desc
        options.append(option)
    return {"provider": "codex", "kind": "approval" if approval else "picker",
            "question": context.split("\n")[0] if context else "Choose an option",
            "context": context, "options": options, "multi": False,
            "input": {"allowed": False, "visible": False, "text": "", "placeholder": ""},
            "actions": ["choose", "cancel"]}


_CODEX_QUEUE_EPISODES = {}
_CODEX_QUESTION_KEYS = {"alt_up": "\x1b[1;3A", "shift_left": "\x1b[1;2D",
                        "alt_down": "\x1b[1;3B", "shift_right": "\x1b[1;2C"}


def _codex_question_key(hint, direction):
    hint = re.sub(r"\s+", "", hint).lower().replace("option", "alt").replace("⌥", "alt").replace("⇧", "shift")
    if direction == "forward":
        return "alt_up" if hint in ("alt+↑", "alt+up") else "shift_left" if hint in ("shift+←", "shift+left") else None
    return "alt_down" if hint in ("alt+↓", "alt+down") else "shift_right" if hint in ("shift+→", "shift+right") else None


def _codex_async_prompt(text):
    lines = text.splitlines()
    footers = [i for i, line in enumerate(lines) if re.search(r"\benter\s+submit\b", line, re.I)]
    if not footers:
        return None
    end = footers[-1]
    footer = "\n".join(lines[end:])
    if not re.search(r"ctrl\s*\+\s*\]\s+skip", footer, re.I):
        return None
    back = re.search(r"([^\n·]+?)\s+(main prompt|prev question)", footer, re.I)
    if not back or any(line.lstrip().startswith("›") for line in lines[end + 1:]):
        return None
    # Each hint can share a footer line, separated by three spaces.
    back_hint = re.split(r"\s{2,}", back[1].strip())[-1]
    back_key = _codex_question_key(back_hint, "back")
    forward = re.search(r"([^\n·]+?)\s+next question", footer, re.I)
    forward_key = _codex_question_key(re.split(r"\s{2,}", forward[1].strip())[-1], "forward") if forward else None
    run = _last_numbered_run(lines[:end])
    if not run:
        return None
    start = run[0][0]
    parts, first = [], start
    for i in range(start - 1, -1, -1):
        value = lines[i].strip()
        if not value and parts:
            break
        if value:
            parts.append(value)
            first = i
    question = "\n".join(reversed(parts))
    progress = re.match(r"^(\d+) of (\d+)\n", question)
    if progress:
        index, count = int(progress[1]), int(progress[2])
        question = question[progress.end():]
    else:
        above = next((line.strip() for line in reversed(lines[:first]) if line.strip()), "")
        progress = re.fullmatch(r"(\d+) of (\d+)", above)
        index, count = (int(progress[1]), int(progress[2])) if progress else (1, 1)
    if not question or not 1 <= index <= count:
        return None
    options = []
    for i, (_, key, label, selected, tail) in enumerate(run):
        value = " ".join(filter(None, [label, tail]))
        options.append({"key": key, "label": value, "selected": selected, "checked": False, "index": i})
    other = options[-1]
    draft = other["label"] if other["label"] != "Other" else ""
    other.update(label="Other", custom=True)
    actions = ["submit", "skip"]
    if back_key:
        actions.append("close")
        if back[2] == "prev question":
            actions.append("previous")
    if forward_key:
        actions.append("next")
    return {"provider": "codex", "kind": "question", "variant": "async", "question": question,
            "context": "", "options": options, "multi": False,
            "question_index": index, "question_count": count,
            "input": {"allowed": True, "visible": other["selected"], "text": draft,
                      "placeholder": "Type your answer"}, "actions": actions,
            "navigation": {"back_key": back_key, "forward_key": forward_key}}


def detect_queued_questions(text, provider=None, uuid=None):
    queued = None
    if provider == "codex" and not _codex_prompt(text):
        lines = text.splitlines()
        headers = [i for i, line in enumerate(lines) if line.strip() == "Queued follow-up inputs"]
        if headers:
            tail = lines[headers[-1] + 1:]
            summary = next((i for i, line in enumerate(tail) if re.fullmatch(
                r"\s*\?\s*(\d+) questions?(?:\s*·.*)?\s*", line)), None)
            if summary is not None:
                count = int(re.search(r"\d+", tail[summary])[0])
                shortcut = re.fullmatch(r"\s*(.+?)\s+to answer\s*", tail[summary + 1]) if len(tail) > summary + 1 else None
                # The summary must belong to the live composer area, not to an
                # earlier transcript quotation. Only native chrome may follow.
                remaining = tail[summary + 2:]
                live = all(not line.strip() or line.lstrip().startswith(("›", "↳"))
                           or re.search(r"context left|for shortcuts|esc to interrupt|gpt-\S+.*·", line, re.I)
                           or BOX_RULE_RE.match(line.strip())
                           or re.fullmatch(r"[\s\u2800-\u28ff]+", line) for line in remaining)
                if count and shortcut and live:
                    queued = {"count": count, "open_key": _codex_question_key(shortcut[1], "forward")}
    if uuid:
        pane = uuid.upper()
        if queued is None:
            _CODEX_QUEUE_EPISODES.pop(pane, None)
        else:
            old = _CODEX_QUEUE_EPISODES.get(pane)
            signature = (queued["count"], queued["open_key"])
            if not old or old[0] != signature:
                old = (signature, secrets.token_hex(16))
                _CODEX_QUEUE_EPISODES[pane] = old
            queued["id"] = old[1]
    return queued


_EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# Harness-injected user lines that aren't something a human typed — excluded from
# the prompt count, mirroring cc-dashboard's _NOISE filter.
_PROMPT_NOISE = ("Caveat:", "<command-name>", "<command-message>", "<local-command",
                 "[Request interrupted", "system-reminder", "<user-prompt-submit",
                 # harness-injected turns the other clients open a session with —
                 # same list cc-dashboard filters on, so the counts agree
                 "task-notification", "<environment_context", "<channel",
                 "# AGENTS.md instructions", "<INSTRUCTIONS>")
# The harness staples these onto ordinary user turns — CLAUDE.md context, memory
# recalls, task nudges — so their presence says nothing about who typed the turn.
# They come off before the noise test; what is left is what the human actually
# sent. A Telegram-relayed prompt is likewise a real prompt, so its envelope is
# stripped rather than treated as noise.
_WRAPPERS = re.compile(
    r"<system-reminder>.*?</system-reminder>|<channel\b[^>]*>|</channel>", re.S)
_USER_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)


def human_prompt(txt):
    """The human-typed part of a user turn, or "" if the turn wasn't one."""
    txt = _WRAPPERS.sub("", txt or "").strip()
    if not txt or any(s in txt for s in _PROMPT_NOISE):
        return ""
    return txt


_OPS = {}   # transcript path -> {"off","files","seen","prompts","fn","pn","action"}


def _short_action(tool, inp):
    """A compact arg for the fleet bubble from a tool_use input dict: the command
    for Bash, the basename for file tools, the query/target otherwise."""
    if tool == "Bash":
        return (inp.get("command") or "").strip().splitlines()[0][:40] if inp.get("command") else ""
    for k in ("file_path", "notebook_path", "path"):
        if inp.get(k):
            return os.path.basename(str(inp[k]).rstrip("/"))[:28]
    for k in ("pattern", "url", "query", "description", "prompt", "subagent_type"):
        if inp.get(k):
            return str(inp[k]).strip().splitlines()[0][:32] if str(inp[k]).strip() else ""
    return ""


def session_ops(path):
    """(files_edited, prompts) for a session, read straight from its transcript.

    Incremental like cc-dashboard's ops scan: each transcript's byte offset is
    remembered and only newly appended bytes are parsed, so the fleet loop never
    re-reads a multi-MB file. First sight is bounded to the last ~4MB so a huge
    backlog can't stall a frame (older ops may be missed — a glance, not an audit).
    A prompt is a non-meta user message carrying real text (tool-result user lines
    and harness noise don't count), deduped by message uuid."""
    st = _OPS.get(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        return ((st or {}).get("fn", 0), (st or {}).get("pn", 0))
    if st is None or size < st["off"]:            # new, or shrank (compaction/clear)
        st = {"off": max(0, size - 4_000_000), "files": set(),
              "seen": set(), "prompts": 0, "fn": 0, "pn": 0, "action": None}
    if size > st["off"]:
        try:
            with open(path, "rb") as fh:
                fh.seek(st["off"]); data = fh.read()
        except OSError:
            data = b""
        cut = data.rfind(b"\n") + 1               # only whole lines
        st["off"] += cut
        for line in data[:cut].decode("utf-8", "ignore").splitlines():
            if '"tool_use"' not in line and '"type":"user"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            # prompts: a real user turn (text, not meta, not a tool result)
            if o.get("type") == "user" and not o.get("isMeta"):
                ct = (o.get("message") or {}).get("content")
                txt = (ct if isinstance(ct, str) else
                       " ".join(b.get("text", "") for b in ct
                                if isinstance(b, dict) and b.get("type") == "text")
                       if isinstance(ct, list) else "")
                if human_prompt(txt):
                    uid = o.get("uuid")
                    if uid is None or uid not in st["seen"]:
                        if uid is not None:
                            st["seen"].add(uid)
                        st["prompts"] += 1
            m = o.get("message")
            if not isinstance(m, dict):
                continue
            for b in (m.get("content") or []):
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                name = b.get("name")
                inp = b.get("input") or {}
                if name in _EDIT_TOOLS:
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if fp:
                        st["files"].add(fp)
                # Newest tool call = what the pane is doing right now. Sourced
                # from the transcript (structured, reliable) rather than scraped
                # off the scrolling screen, which loses the ⏺ line under output.
                if name:
                    st["action"] = {"tool": name,
                                    "arg": _short_action(name, inp)}
    st["fn"], st["pn"] = len(st["files"]), st["prompts"]
    _OPS[path] = st
    return st["fn"], st["pn"]


# ── Codex / Grok journals ───────────────────────────────────────────────────
# Neither client writes a Claude-shaped transcript, and the fleet record their
# hooks leave carries no counters at all — the numbers live in the client's own
# append-only journal. These are the same files CC-Dash reads, scanned the same
# way, so both dashboards always agree about a session.
CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")
GROK_SESSIONS = os.path.expanduser("~/.grok/sessions")
GROK_LOG = os.path.expanduser("~/.grok/logs/unified.jsonl")
_journals = {}                   # (provider, sid) -> path, cached: the glob is not free
_NATIVE = {}                     # path -> incremental scan state


def journal_path(provider, sid):
    """The client's own session journal — "" when it has not appeared yet."""
    key = (provider, sid)
    hit = _journals.get(key)
    if hit and os.path.exists(hit):
        return hit
    if provider == "codex":
        found = glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", f"*-{sid}.jsonl"))
    elif provider == "grok":
        found = glob.glob(os.path.join(GROK_SESSIONS, "*", sid, "chat_history.jsonl"))
    else:
        found = []
    path = found[-1] if found else ""
    if path:
        _journals[key] = path
    return path


def _fresh_native():
    return {"off": 0, "files": set(), "prompts": 0, "add": 0, "del": 0,
            "action": None, "stamp": None, "model": None, "effort": None,
            "mode": None, "tokens": 0, "cost": 0.0, "ctx": None}


_CODEX_PRICING = {
    # USD per million: uncached input, cached input, cache write, output.
    "gpt-6-astra": (10.0, 1.0, 12.5, 50.0),
    "gpt-5.6-sol": (4.0, .4, 5.0, 20.0),
    "gpt-5.6-terra": (2.0, .2, 2.5, 12.0),
    "gpt-5.6-luna": (.2, .02, .25, 1.2),
    "gpt-5.5": (5.0, .5, 0.0, 30.0),
    "gpt-5.3": (1.75, .175, 0.0, 14.0),
}


def _codex_cost(model, inp, cached, cache_write, out):
    """API-equivalent USD for Codex tokens; unknown models stay unpriced."""
    name = (model or "").lower().replace("-build", "")
    if name == "gpt-5.6":
        name = "gpt-5.6-sol"
    rate = next((v for k, v in _CODEX_PRICING.items() if name.startswith(k)), None)
    if not rate:
        return 0.0
    pin, pcached, pwrite, pout = rate
    uncached = max(0, (inp or 0) - (cached or 0))
    return (uncached * pin + (cached or 0) * pcached
            + (cache_write or 0) * pwrite + (out or 0) * pout) / 1e6


def _codex_permission_mode(context):
    """Codex turn_context permissions in the same three labels as its picker."""
    profile = context.get("permission_profile") or {}
    sandbox = context.get("sandbox_policy") or {}
    profile_type = profile.get("type") if isinstance(profile, dict) else profile
    sandbox_type = sandbox.get("type") if isinstance(sandbox, dict) else sandbox
    if profile_type == "disabled" or sandbox_type == "danger-full-access":
        return "Full access"
    if sandbox_type == "read-only" or profile_type == "read-only":
        return "Read only"
    return "Default"


def codex_ops(path):
    """Prompts, edited files and line deltas from a Codex rollout journal.

    Incremental, like session_ops: only bytes appended since the last read are
    parsed. Edits arrive as apply_patch payloads inside a custom_tool_call, so the
    file list and +/- counts come from the patch body itself.
    """
    st = _NATIVE.get(path) or _fresh_native()
    try:
        size = os.path.getsize(path)
    except OSError:
        return st
    if size < st["off"]:                       # truncated/rotated — start over
        st = _fresh_native()
    if size > st["off"]:
        try:
            with open(path, "rb") as fh:
                fh.seek(st["off"]); data = fh.read()
        except OSError:
            data = b""
        cut = data.rfind(b"\n") + 1
        st["off"] += cut
        for line in data[:cut].decode("utf-8", "ignore").splitlines():
            try:
                rec = json.loads(line) or {}
                p = rec.get("payload") or {}
            except Exception:
                continue
            typ = p.get("type") or rec.get("type")
            if typ == "turn_context":
                st["model"] = p.get("model") or st.get("model")
                collaboration = p.get("collaboration_mode") or {}
                settings = collaboration.get("settings") or {}
                st["effort"] = (p.get("effort") or p.get("reasoning_effort")
                                or settings.get("reasoning_effort") or st.get("effort"))
                st["mode"] = _codex_permission_mode(p)
                continue
            if typ == "token_count":
                info = p.get("info") if isinstance(p.get("info"), dict) else {}
                last = info.get("last_token_usage") if isinstance(
                    info.get("last_token_usage"), dict) else {}
                total = last.get("total_tokens")
                window = info.get("model_context_window")
                if not isinstance(total, bool) and not isinstance(window, bool):
                    try:
                        total, window = float(total), float(window)
                        if math.isfinite(total) and math.isfinite(window) and window > 0:
                            st["ctx"] = round(max(0.0, min(100.0,
                                                          total / window * 100)))
                    except (TypeError, ValueError):
                        pass
                u = info.get("total_token_usage") if isinstance(
                    info.get("total_token_usage"), dict) else {}
                i = u.get("input_tokens") or 0
                c = u.get("cached_input_tokens") or 0
                w = u.get("cache_write_input_tokens") or 0
                o = u.get("output_tokens") or 0
                st["tokens"] = _produced_tokens(i, c, w, o)
                st["cost"] = _codex_cost(st.get("model"), i, c, w, o)
                continue
            if typ == "message" and p.get("role") == "user":
                txt = " ".join(x.get("text", "") for x in (p.get("content") or [])
                               if isinstance(x, dict) and x.get("type") == "input_text")
                if txt.strip() and not any(s in txt for s in _PROMPT_NOISE):
                    st["prompts"] += 1
                continue
            if typ != "custom_tool_call":
                continue
            name = p.get("name") or "tool"
            raw = p.get("input") or ""
            if not isinstance(raw, str):
                raw = ""
            body = raw.replace("\\n", "\n")
            hit = ""
            if "*** Begin Patch" in body:
                for m in re.finditer(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$",
                                     body, re.M):
                    hit = m.group(1).strip()
                    st["files"].add(hit)
                for change in body.splitlines():
                    if change.startswith(("+++", "---")):
                        continue
                    if change.startswith("+"):
                        st["add"] += 1
                    elif change.startswith("-"):
                        st["del"] += 1
                hit = os.path.basename(hit)
                name = "apply_patch"
            else:
                # Every Codex tool call arrives as a snippet of JS that awaits the
                # real tool, so the first line is boilerplate. Name the inner call
                # instead, and for a shell, the command it is about to run.
                inner = re.search(r"tools\.(\w+)", body)
                if inner:
                    name = inner.group(1)
                cmd = re.search(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"', body)
                if cmd:
                    hit = cmd.group(1).replace('\\"', '"').replace("\\\\", "\\")[:32]
                elif body.strip():
                    hit = body.strip().splitlines()[0][:32]
            st["action"] = {"tool": name, "arg": hit}
    _NATIVE[path] = st
    return st


def grok_ops(path):
    """Prompts from Grok's chat log; file/line deltas from its hunk records.

    The hunk file is rewritten rather than appended, so this re-reads on any
    change of size/mtime instead of tracking an offset.
    """
    hunk = os.path.join(os.path.dirname(path), "hunk_records.jsonl")
    try:
        stamp = (os.path.getmtime(path), os.path.getsize(path),
                 os.path.getmtime(hunk) if os.path.exists(hunk) else 0)
    except OSError:
        return _NATIVE.get(path) or _fresh_native()
    st = _NATIVE.get(path)
    if st and st.get("stamp") == stamp:
        return st
    st = _fresh_native(); st["stamp"] = stamp
    try:
        with open(path, errors="ignore") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") == "user":
                    txt = " ".join(x.get("text", "") for x in (o.get("content") or [])
                                   if isinstance(x, dict) and x.get("type") == "text")
                    if txt.strip() and not any(s in txt for s in _PROMPT_NOISE):
                        st["prompts"] += 1
                elif o.get("type") in ("tool_call", "backend_tool_call"):
                    kind = o.get("kind") or {}
                    st["action"] = {"tool": o.get("name")
                                    or kind.get("tool_type") or "tool", "arg": ""}
    except OSError:
        return st
    try:
        with open(hunk, errors="ignore") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                # Agent hunks are marked `agent`; Grok's compensating `removed`
                # records drop authorType but keep agentId. Never count a human edit.
                author = o.get("authorType")
                if author == "human" or (author != "agent" and not o.get("agentId")):
                    continue
                if o.get("eventType") not in ("added", "updated", "removed"):
                    continue
                if o.get("filePath"):
                    st["files"].add(o["filePath"])
                try:
                    added = int(o.get("linesAdded") or 0)
                    removed = int(o.get("linesRemoved") or 0)
                except (TypeError, ValueError):
                    continue
                # Each refinement is a signed delta: a negative "added" is really a
                # deletion of lines this chat inserted earlier, and vice versa.
                st["add"] += added if added >= 0 else 0
                st["del"] += -added if added < 0 else 0
                st["del"] += removed if removed >= 0 else 0
                st["add"] += -removed if removed < 0 else 0
    except OSError:
        pass
    _NATIVE[path] = st
    return st


def native_ops(provider, path):
    return codex_ops(path) if provider == "codex" else grok_ops(path)


# ── live running / idle ─────────────────────────────────────────────────────
# Claude's working/idle .state file is rewritten on every UserPromptSubmit/Stop
# and stays honest. Codex and Grok only touch theirs at lifecycle hooks, which
# miss Stop often enough that the yard latches on the wrong verb.
#
# Each TUI already tells the user a turn is in flight — Codex paints
# "esc to interrupt" on the live progress line, Grok writes turn_started /
# turn_ended into events.jsonl and "Worked for" onto the transcript. Read those
# same signals and overlay them on the hook file. None = no signal, keep the hook.
_TURN_BUSY = {}                      # path -> {off, busy} incremental jsonl scan

# Codex 0.153+ animates the progress glyph between • and ◦; elapsed expands at
# the minute and hour; the composer hint can share the same line. The question
# footer also says "esc to interrupt" but never with an elapsed `(12s • …)`.
# Older builds painted a bare "Working (esc to interrupt)" — still match that,
# but only as that exact phrase so a question does not look like a live turn.
_CODEX_PROGRESS_RE = re.compile(
    r"(?:"
    r"[•◦][^\n]*\((?:(?:\d+h\s+)?\d+m\s+)?\d+s\s*•\s*esc to interrupt\)"
    r"|Working \(esc to interrupt\)"
    r")",
    re.I,
)

_GROK_INTERRUPT_RE = re.compile(
    r"ctrl\+c to (?:interrupt|cancel)|send a message to interrupt|"
    r"\bstill running\b",
    re.I,
)
# Live footer Grok paints while a turn runs: spinner + elapsed + `[stop]`.
# Leftover `◈ tool` rows stay on the alt-screen after the turn ends and must
# not count as busy — that is what latched the yard on "working".
_GROK_PROGRESS_RE = re.compile(
    r"(?:"
    r"\[stop\]"
    r"|[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏].{0,80}\b\d+(?:\.\d+)?\s*s\b"
    r")",
    re.I,
)
_GROK_COMPACT_Q = re.compile(
    r"^\s*#\d+\s+.+\(\+\d+\s+lines?\)", re.M)
_GROK_DONE_RE = re.compile(r"^\s*Worked for\s+", re.I)
# In-flight thinking only. `◆ Thought for 1.4s` and `◈ read_file` are history.
_GROK_LIVE_RE = re.compile(
    r"^\s*(?:◆\s*Thinking\b|Thinking\.\.\.)",
    re.I,
)


def _turn_busy_scan(path, classify):
    """Incremental jsonl scan: True if the last start/end event was a start.

    classify(obj) -> 'start' | 'end' | None. Missing/empty file, or a file that
    has never logged a turn edge, returns None so the caller can fall through.
    """
    if not path:
        return None
    st = _TURN_BUSY.get(path) or {"off": 0, "busy": None}
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size < st["off"]:
        st = {"off": 0, "busy": None}
    if size > st["off"]:
        try:
            with open(path, "rb") as fh:
                fh.seek(st["off"])
                data = fh.read()
        except OSError:
            return st.get("busy")
        cut = data.rfind(b"\n") + 1
        st["off"] += cut
        for line in data[:cut].decode("utf-8", "ignore").splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            kind = classify(obj)
            if kind == "start":
                st["busy"] = True
            elif kind == "end":
                st["busy"] = False
        _TURN_BUSY[path] = st
    return st.get("busy")


def _codex_turn_kind(obj):
    if (obj or {}).get("type") != "event_msg":
        return None
    typ = ((obj.get("payload") or {}).get("type") or "")
    if typ == "task_started":
        return "start"
    if typ in ("task_complete", "turn_aborted"):
        return "end"
    return None


def _grok_turn_kind(obj):
    typ = (obj or {}).get("type")
    if typ == "turn_started":
        return "start"
    if typ == "turn_ended":
        return "end"
    return None


def _grok_events_path(transcript):
    """events.jsonl sits next to the chat_history.jsonl journal_path returns."""
    if not transcript:
        return ""
    path = os.path.join(os.path.dirname(transcript), "events.jsonl")
    return path if os.path.exists(path) else ""


def _grok_transcript(text):
    """Visible Grok chat above the composer box."""
    lines = [_grok_clean(l) for l in (text or "").splitlines()]
    end = next((i for i in range(len(lines) - 1, -1, -1)
                if _GROK_STATUS.search(lines[i])), None)
    if end is None:
        return lines
    start = end
    while start > 0 and "╭" not in lines[start]:
        start -= 1
    return lines[:start] if "╭" in lines[start] else lines[:end]


def _grok_busy(text):
    """True when Grok's live frame is showing an in-flight turn."""
    raw = text or ""
    if _GROK_PROGRESS_RE.search(raw) or _GROK_INTERRUPT_RE.search(raw):
        return True
    if _GROK_COMPACT_Q.search(raw):
        return True
    if _grok_prompt(raw):
        return False
    last_done = last_live = -1
    for i, line in enumerate(_grok_transcript(raw)):
        if _GROK_DONE_RE.match(line):
            last_done = i
        if _GROK_LIVE_RE.match(line):
            last_live = i
    return last_live > last_done


def _grok_idle(text):
    if detect_prompt(text, "grok") or _grok_busy(text):
        return False
    ghost, draft = read_input_box(text)
    return (not draft) or ghost


def detect_running(provider, text, transcript=""):
    """Whether this pane is mid-turn. True / False / None (no signal).

    Screen chrome wins when it is decisive: that is what the user is looking
    at. Grok's live status line (`[stop]`, spinner + elapsed) is the working
    signal; an idle composer without it is stopped, even if events.jsonl
    missed turn_ended. The journal is only the fallback when the screen is
    ambiguous. Codex's rollout task_started/task_complete is the same idea
    when the progress line is off the live tail.
    """
    if provider == "codex":
        if _codex_busy(text):
            return True
        journal = _turn_busy_scan(transcript, _codex_turn_kind) if transcript else None
        if journal is True:
            return True
        if _codex_idle(text):
            return False
        return journal
    if provider == "grok":
        if _grok_busy(text):
            return True
        if _grok_idle(text):
            return False
        journal = _turn_busy_scan(_grok_events_path(transcript), _grok_turn_kind)
        if journal is True:
            return True
        if journal is False:
            return False
        return None
    return None


REAP_AFTER = 3 * 3600            # the reaper button's cutoff: sessions this old


def session_start(transcript, fallback):
    """When a session actually BEGAN, as a unix time.

    Nothing else in a fleet record dates the session: the statusline dump is
    rewritten on every render and the .state file flips on every hook, so both
    only say "recently alive". The transcript is created once, when the chat
    opens, and is append-only after that — its birth time is the honest start
    clock (a /clear opens a new one, which is the right answer too). Falls back
    to the record's own mtime when there is no transcript to stat.
    """
    if transcript:
        try:
            st = os.stat(transcript)
            return getattr(st, "st_birthtime", None) or st.st_mtime
        except OSError:
            pass
    return fallback


def read_fleet_files():
    """Status dumps written by ~/.claude/statusline.sh + cc-active.sh.

    We only read these; cc-dashboard.py owns them and prunes its own staleness.
    """
    now, out = time.time(), {}
    _FLEET_BY_TTY.clear()
    for p in glob.glob(os.path.join(FLEET_DIR, "*.json")):
        try:
            mt = os.path.getmtime(p)
            d = json.load(open(p))
        except Exception:
            continue
        provider = d.get("provider") or DEFAULT_PROVIDER
        key = d.get("fleet_key") or pathlib.Path(p).stem
        state = "idle"
        state_mt = None                       # mtime of the winning .state file
        sf = os.path.join(FLEET_DIR, f"claude-{key}.state")
        for cand in (sf, os.path.join(FLEET_DIR, f"{key}.state")):
            if os.path.exists(cand):
                try:
                    state = open(cand).read().strip() or "idle"
                    state_mt = os.path.getmtime(cand)
                    break
                except Exception:
                    pass
        # Claude's statusline rewrites its dump on every render, so 90s of silence
        # means the pane is gone. Codex and Grok only touch their record at
        # lifecycle events — a chat left open overnight writes nothing — so they
        # get CC-Dash's far longer retention, and the pane list is what actually
        # retires them: a record with no live pane never becomes a row.
        limit = STALE if provider == DEFAULT_PROVIDER else (
            STALE_ENDED if state == "ended" else STALE_GENERIC)
        if now - max(mt, state_mt or mt) > limit:
            continue
        pane = d.get("iterm_pane") or ""
        uuid = pane.split(":")[-1].upper() if ":" in pane else ""
        cw = d.get("context_window") or {}
        cst = d.get("cost") or {}
        # Claude names its transcript in the record; the others keep their own
        # journal, which we locate from the session id exactly as CC-Dash does.
        tp = d.get("transcript_path") or ""
        if not tp and provider != DEFAULT_PROVIDER:
            tp = journal_path(provider, d.get("session_id") or "")
        row = {
            "sid": key,
            "provider": provider,
            "transcript": tp,
            "state": state,
            # When this pane entered its current state, straight from the .state
            # file the working/idle hooks touch — the SAME clock cc-dashboard and
            # the iTerm tab colour use, so the phone timer matches them and, being
            # on disk, survives a server restart.
            "state_since": state_mt,
            "cwd": (d.get("workspace") or {}).get("current_dir", "") or d.get("cwd", ""),
            "model": (d.get("model") or {}).get("display_name", ""),
            "cost": round(cst.get("total_cost_usd", 0) or 0, 2),
            "ctx": cw.get("used_percentage"),
            "limits": d.get("rate_limits") or {},
            "effort": (d.get("effort") or {}).get("level"),
            "mtime": mt,
            # richer per-agent metrics, ported from cc-dashboard's fleet row
            "tokens": (cw.get("total_input_tokens") or 0)
                    + (cw.get("total_output_tokens") or 0),
            "lines_add": cst.get("total_lines_added") or 0,
            "lines_del": cst.get("total_lines_removed") or 0,
            "dur_ms": cst.get("total_duration_ms") or 0,
            "age": int(now - mt),
            "started": session_start(tp, mt),
        }
        if provider == DEFAULT_PROVIDER:
            fcount, pcount = session_ops(tp) if tp else (0, 0)
            row["files"] = fcount
            row["prompts"] = pcount
            row["action"] = (_OPS.get(tp) or {}).get("action") if tp else None
            # subagents currently alive for this pane — one pet each on the yard
            row["subs"] = live_subagents(tp, now) if tp else []
        else:
            # Codex and Grok keep their own accounting in their own journal; their
            # hook record carries none. No subagent transcripts to watch, either.
            st = native_ops(provider, tp) if tp else None
            row["files"] = len(st["files"]) if st else 0
            row["prompts"] = st["prompts"] if st else 0
            row["lines_add"] = st["add"] if st else 0
            row["lines_del"] = st["del"] if st else 0
            row["action"] = (st or {}).get("action")
            if provider == "codex" and row["ctx"] is None and st:
                row["ctx"] = st.get("ctx")
            row["subs"] = []
            if provider == "codex" and st:
                row["model"] = row["model"] or st.get("model") or ""
                row["effort"] = row["effort"] or st.get("effort")
                row["mode"] = st.get("mode")
                if row["ctx"] is None:
                    row["ctx"] = st.get("ctx")
                row["tokens"] = st.get("tokens") or 0
                row["cost"] = round(st.get("cost") or 0, 2)
            # Grok's fleet dump has no cost/context; the session dir does.
            if provider == "grok" and tp:
                sdir = os.path.dirname(tp)
                try:
                    sig = json.load(open(os.path.join(sdir, "signals.json")))
                    if sig.get("contextWindowUsage") is not None:
                        row["ctx"] = sig["contextWindowUsage"]
                except Exception:
                    pass
                try:
                    sess = (json.load(open(os.path.join(sdir, "usage.json")))
                            or {}).get("session") or {}
                    row["tokens"] = _produced_tokens(
                        sess.get("inputTokens") or 0,
                        sess.get("cachedReadTokens") or 0,
                        sess.get("cacheCreationTokens") or 0,
                        sess.get("outputTokens") or 0)
                    row["cost"] = round(
                        (sess.get("costUsdTicks") or 0) / GROK_COST_TICKS, 2)
                except Exception:
                    pass
        # A hook child can lose ITERM_SESSION_ID, so a record may name only the tty
        # it was written from. Index those by tty and let build_fleet match them to
        # the pane it is already looking at.
        # One pane outlives the chats that run in it: a /clear, a --resume or just
        # a new session leaves the previous chat's record behind, still naming this
        # pane and still inside its stale window. Keyed blindly, whichever record
        # glob happened to yield last would win — often the dead one, so the pane
        # showed a finished chat's prompts, lines and cost. The live chat is the
        # one whose record was written most recently, so freshest wins.
        if uuid:
            prev = out.get(uuid)
            if prev is None or mt >= prev["mtime"]:
                out[uuid] = row
        elif d.get("tty"):
            key = "/dev/" + os.path.basename(d["tty"])
            prev = _FLEET_BY_TTY.get(key)
            if prev is None or mt >= prev["mtime"]:
                _FLEET_BY_TTY[key] = row
    return out


# A subagent (Task/Agent tool call) gets its own transcript under
# <session>/subagents/agent-*.jsonl while it runs; it keeps appending as the agent
# works and goes quiet the moment it finishes. So "alive" = the file was written
# within the last few seconds. This catches both foreground and background spawns
# (a backgrounded agent's launch is acked instantly in the main transcript, so it
# leaves no in-flight marker there) and self-clears when the agent stops.
SUB_ACTIVE = 15   # seconds since last write to still count a subagent as running


def live_subagents(transcript_path, now):
    """agentTypes of subagents still writing under this session, newest first."""
    sdir = os.path.splitext(transcript_path)[0] + "/subagents"
    out = []
    try:
        entries = os.listdir(sdir)
    except OSError:
        return out
    for fn in entries:
        if not fn.endswith(".jsonl"):
            continue
        fp = os.path.join(sdir, fn)
        try:
            mt = os.path.getmtime(fp)
        except OSError:
            continue
        if now - mt >= SUB_ACTIVE:
            continue
        kind = "agent"
        try:
            meta = json.load(open(fp[:-6] + ".meta.json"))
            kind = meta.get("agentType") or kind
        except Exception:
            pass
        out.append((mt, kind))
    out.sort(reverse=True)          # freshest first
    return [k for _, k in out]


def fleet_limits(files):
    """Account-wide 5h/7d usage, pooled across every pane's copy.

    A pane only refreshes its reading when it makes an API call, so no single
    dump is authoritative. Two failure modes to dodge:

      * an expired window — a pane idle since yesterday still rewrites its dump
        every render, carrying a reading whose window closed hours ago;
      * a placeholder — a just-started pane reports 0% against a window it
        synthesised (this hour + 5h) before any rate-limit header arrived.

    Picking the newest resets_at hits the placeholder every time and shows 0%
    while the account is really at 17%. So: drop closed windows, then take the
    highest percentage still open. Usage only climbs inside a window, so every
    stale-but-open reading is a lower bound on the truth and the max is the
    freshest fact anyone has. Each window is resolved on its own — the 5h
    winner must not drag an unrelated 7d reading along with it.
    """
    now, out = time.time(), {}
    for window in ("five_hour", "seven_day"):
        best = None
        for f in files.values():
            w = (f.get("limits") or {}).get(window)
            if not isinstance(w, dict):
                continue
            resets = w.get("resets_at") or 0
            if resets and resets <= now:          # window already rolled over
                continue
            pct = w.get("used_percentage")
            if pct is None:
                continue
            if best is None or pct > best.get("used_percentage", -1):
                best = w
        if best is not None:
            out[window] = best
    return out


# ── live working directory ─────────────────────────────────────────────────
# The critter name tracks the dir the session is CURRENTLY in, not the launch dir.
# Ported from cc-dashboard's latest_cwd so the phone and the TUI name panes the same.
def tail_lines(path, n, size=524288):
    # last n lines without reading the whole (tens-of-MB) transcript
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); sz = f.tell()
            f.seek(max(0, sz - size))
            data = f.read()
        return data.decode("utf-8", "ignore").splitlines()[-n:]
    except OSError:
        return []

def _is_scratch(p):
    # a temp/scratch launch dir is never the repo the session is really working in
    return bool(p) and ("cc-scratch" in p or "/var/folders/" in p
                        or p.startswith("/tmp") or p.startswith("/private/tmp"))

def _cd_target(cmd):
    # destination of the last absolute `cd <dir>` in a (possibly compound) command
    best = None
    for m in re.finditer(r'(?:^|[;&|]|&&)\s*cd\s+("([^"]+)"|\'([^\']+)\'|([^\s;&|]+))', cmd):
        t = m.group(2) or m.group(3) or m.group(4)
        if t and t.startswith("/"): best = t.rstrip("/")
    return best

_cwd_cache = {}   # transcript path -> ((mtime,size), cwd)
def latest_cwd(path):
    # The dir the session is CURRENTLY working in: normally the transcript's per-entry
    # `cwd` (follows the session across dirs, unlike statusline's launch-pinned
    # workspace.current_dir). A session launched in a scratch dir keeps that scratch cwd
    # even while editing a real repo, so when cwd is scratch we recover the working dir
    # from recent `cd /repo` moves and, failing that, the dir of the files it's touching.
    if not path:
        return None
    try: st = os.stat(path)
    except OSError: return None
    key = (st.st_mtime, st.st_size)
    hit = _cwd_cache.get(path)
    if hit and hit[0] == key: return hit[1]
    base = None; cd_hint = None; file_hint = None
    for line in reversed(tail_lines(path, 80)):
        try: o = json.loads(line)
        except Exception: continue
        if base is None and o.get("cwd"): base = o["cwd"]
        if base and not _is_scratch(base): break
        for b in ((o.get("message") or {}).get("content") or []):
            if not (isinstance(b, dict) and b.get("type") == "tool_use"): continue
            inp = b.get("input") or {}
            if cd_hint is None and isinstance(inp.get("command"), str):
                cd_hint = _cd_target(inp["command"])
            fp = inp.get("file_path") or inp.get("path")
            if file_hint is None and isinstance(fp, str) and fp.startswith("/") and not _is_scratch(fp):
                file_hint = os.path.dirname(fp.rstrip("/"))
        if cd_hint: break
    cwd = base
    if base and _is_scratch(base):
        cwd = cd_hint or file_hint or base
    _cwd_cache[path] = (key, cwd)
    return cwd


# ── file churn (what a chat actually changed on disk) ──────────────────────
# The statusline's cost.total_lines_added/removed counts only Claude's OWN
# Edit/Write applications, and session_ops() counts only its Edit/Write tool
# calls. Measured on this machine: 658 Bash calls against 7 Edits across the
# last 40 transcripts — nearly every change here is a shell redirect, sed or
# heredoc, so both counters sat at 0 and the usage cards showed "0 files · +0
# −0" for sessions that had rewritten hundreds of lines. Ask git instead: it
# sees the file, not the tool that wrote it.
_GIT_TTL = 12                # seconds a repo reading is reused across panes
_GIT_MAX_UNTRACKED = 200     # line-count at most this many new files per repo
_GIT_MAX_BYTES = 2_000_000   # ...and skip any single one bigger than this
_git_cache = {}              # cwd -> (ts, {path: (add, del)} | None)
_churn = {}                  # session -> {"add", "del", "files", "last"}
# Totals outlive the process: this server hot-reloads on every deploy, and a
# chat that has been running for an hour must not have its counters zeroed
# (which would look exactly like the bug this replaced). Only the running
# totals persist — the per-file "last" snapshot is rebuilt on first reading
# after a restart, which re-bases silently and so cannot double-count.
_CHURN_FILE = HERE / ".churn.json"
_CHURN_KEEP = 14 * 86400     # forget a session untouched for two weeks
_churn_saved = 0.0


def _load_churn():
    try:
        raw = json.loads(_CHURN_FILE.read_text())
    except Exception:
        return
    cutoff = time.time() - _CHURN_KEEP
    for key, v in (raw or {}).items():
        if not isinstance(v, dict) or (v.get("ts") or 0) < cutoff:
            continue
        _churn[key] = {"add": v.get("add") or 0, "del": v.get("del") or 0,
                       "files": set(v.get("files") or []), "last": None,
                       "ts": v.get("ts")}


def _save_churn():
    global _churn_saved
    _churn_saved = time.time()
    try:
        tmp = _CHURN_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(
            {k: {"add": v["add"], "del": v["del"],
                 "files": sorted(v["files"]), "ts": v.get("ts") or _churn_saved}
             for k, v in _churn.items()}))
        tmp.replace(_CHURN_FILE)
        _CHURN_FILE.chmod(0o600)     # holds file paths, like the summary cache
    except Exception as e:
        print(f"  [churn] save failed: {type(e).__name__}: {e}", flush=True)


async def _git(cwd, *args):
    """stdout of a git command in cwd, or None if it fails or isn't a repo."""
    try:
        p = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 5)
    except (TimeoutError, OSError, ValueError):
        return None
    return out.decode("utf-8", "ignore") if p.returncode == 0 else None


def _count_untracked(cwd, paths):
    """{path: (lines, 0)} for new files — the whole file is added work."""
    out = {}
    for path in paths:
        full = os.path.join(cwd, path)
        try:
            if os.path.getsize(full) > _GIT_MAX_BYTES:
                out[path] = (0, 0)
                continue
            body = open(full, "rb").read()
        except OSError:
            continue
        if b"\0" in body:                       # binary: a touched path, no lines
            out[path] = (0, 0)
        else:
            out[path] = (body.count(b"\n") + (0 if body.endswith(b"\n") or not body else 1), 0)
    return out


async def git_snapshot(cwd):
    """{path: (added, removed)} for everything uncommitted in cwd, or None.

    Tracked files come from --numstat; a binary one reports "-" and counts as a
    touched path with no lines. Untracked files count every line as added, since
    the whole file is new work. Cached per repo — several panes share one cwd.
    """
    if not cwd or not os.path.isdir(cwd):
        return None
    hit = _git_cache.get(cwd)
    if hit and time.time() - hit[0] < _GIT_TTL:
        return hit[1]
    snap = None
    # vs HEAD covers staged + unstaged; a repo with no commits yet has no HEAD,
    # so fall back to the index-only diff rather than reporting nothing.
    numstat = await _git(cwd, "diff", "--numstat", "HEAD")
    if numstat is None:
        numstat = await _git(cwd, "diff", "--numstat")
    if numstat is not None:
        snap = {}
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            a, d, path = parts
            snap[path] = (int(a) if a.isdigit() else 0,
                          int(d) if d.isdigit() else 0)
        others = (await _git(cwd, "ls-files", "--others", "--exclude-standard") or "")
        # Reading the new files is the slow half (290ms in one of this fleet's
        # repos) and it is plain blocking IO, so it goes to a thread — the pane
        # stream shares this loop and must not stutter every refresh.
        snap.update(await asyncio.to_thread(
            _count_untracked, cwd, others.splitlines()[:_GIT_MAX_UNTRACKED]))
    _git_cache[cwd] = (time.time(), snap)
    return snap


_load_churn()


def churn_for(key, snap):
    """Per-chat {add, del, files} accumulated from repo snapshots, or None.

    Reporting the raw working-tree diff would be wrong twice over: it credits a
    chat with dirt that was already there, and a commit would wipe the numbers
    back to zero mid-session. So accumulate GROWTH per file — what went up since
    the last reading — and re-base silently when a path shrinks or disappears
    (committed, reverted, stashed). Never double-counts, never goes negative.
    First sight only records the baseline, so pre-existing changes stay out.
    """
    if not key or snap is None:
        return None
    c = _churn.setdefault(key, {"add": 0, "del": 0, "files": set(), "last": None})
    grew = False
    if c["last"] is not None:
        for path, (a, d) in snap.items():
            pa, pd = c["last"].get(path, (0, 0))
            if a > pa or d > pd:
                c["add"] += max(0, a - pa)
                c["del"] += max(0, d - pd)
                c["files"].add(path)
                grew = True
    c["last"] = snap
    if grew:
        c["ts"] = time.time()
        if c["ts"] - _churn_saved > 15:      # throttle: a small file, but every poll is silly
            _save_churn()
    return {"add": c["add"], "del": c["del"], "files": len(c["files"])}


_FLEET_CACHE = []      # last full yard, for while a pane is maximized


async def build_fleet():
    sessions = await all_sessions()
    files = read_fleet_files()
    rows = []
    for uuid, s in sessions.items():
        try:
            job = await s.async_get_variable("jobName") or ""
            cwd = await s.async_get_variable("path") or ""
            tty = await s.async_get_variable("tty") or ""
        except Exception:
            job, cwd, tty = "", "", ""
        # A Codex/Grok hook child can lose ITERM_SESSION_ID, leaving a record that
        # knows only the tty it ran on. That still names this pane exactly.
        f = files.get(uuid) or _FLEET_BY_TTY.get(tty)
        txt = await pane_text(s)
        provider = (f or {}).get("provider") or pane_provider(uuid, job, txt)
        if not provider:
            continue                      # hide scratch shells entirely
        KNOWN_AGENTS.setdefault(uuid, provider)
        lines = [l for l in txt.splitlines() if l.strip()]
        grok_st = detect_grok_status(txt) if provider == "grok" else {}
        mode = (detect_mode(txt) if provider == DEFAULT_PROVIDER else
                (f or {}).get("mode") or grok_st.get("mode", ""))
        prompt = detect_prompt(txt, provider, uuid)
        state = (f or {}).get("state", "idle")
        if state != "ended":
            running = detect_running(provider, txt, (f or {}).get("transcript") or "")
            if running is True:
                state = "working"
            elif running is False:
                state = "idle"
        # live working dir: the transcript's current cwd (follows `cd`s), then the
        # statusline's launch-pinned dir, then the iTerm pane path — same order as ccdash
        # Only Claude's transcript records a per-entry cwd; the others pin theirs in
        # the hook record, so their live dir is the pane's own path.
        live_cwd = (latest_cwd((f or {}).get("transcript"))
                    if provider == DEFAULT_PROVIDER else "") or (f or {}).get("cwd") or cwd
        # What this chat has changed on disk. Keyed on the Claude session id so
        # a /clear starts fresh; the pane uuid only stands in when no statusline
        # dump has landed yet. Falls back to the statusline/transcript counters
        # when the pane is not in a git repo.
        ch = churn_for((f or {}).get("sid") or uuid, await git_snapshot(live_cwd))
        # An all-zero churn is "nothing observed yet", not "nothing changed":
        # first sight only records a baseline, and a server restart mid-session
        # re-bases every pane. Treating that as an answer would blank out the
        # statusline's own real counters, so only trust churn once it has seen
        # something move.
        if ch and not (ch["add"] or ch["del"] or ch["files"]):
            ch = None
        rows.append({
            "uuid": uuid,
            "sid": (f or {}).get("sid"),
            "job": job,
            "provider": provider,          # claude | codex | grok — picks the sprite
            "cwd": live_cwd,
            "name": os.path.basename(live_cwd.rstrip("/")) or "?",
            "state": state,
            "model": (f or {}).get("model", "") or grok_st.get("model", ""),
            "ctx": (f or {}).get("ctx"),
            "cost": (f or {}).get("cost"),
            "effort": (f or {}).get("effort") or grok_st.get("effort"),
            "mode": mode,
            "prompt": prompt,
            "sendable": True,
            "tokens": (f or {}).get("tokens"),
            "lines_add": ch["add"] if ch else (f or {}).get("lines_add"),
            "lines_del": ch["del"] if ch else (f or {}).get("lines_del"),
            "files": ch["files"] if ch else (f or {}).get("files"),
            "prompts": (f or {}).get("prompts"),
            "age": (f or {}).get("age"),
            "started": (f or {}).get("started"),
            # background shells this chat still has running — a dev server left
            # up, a build still going. Only Claude announces them, so the others
            # are always 0 here rather than falsely quiet.
            "bg": _BG.get((f or {}).get("transcript") or "", 0),
            "dur_ms": (f or {}).get("dur_ms"),
            "work_since": (f or {}).get("state_since"),
            "mtime": (f or {}).get("mtime"),      # reaper fallback clock
            "action": (f or {}).get("action"),   # newest tool call, from transcript
            "subs": (f or {}).get("subs") or [],  # in-flight subagents → yard pets
            # Enough lines that the current `⏺ Tool(args)` action line is in the
            # window — it sits several lines above the bottom, behind its `⎿`
            # result, the spinner and the prompt box. The bubble distiller scans
            # this backwards for the newest tool call to show what Claude's doing.
            "tail": lines[-14:],
        })
    # Maximizing a watched pane (grow_pane) hides its tab-mates from iTerm's API,
    # which would empty the yard for everyone else while one phone reads a chat.
    # Serve the last full picture for the panes that are merely out of view.
    global _FLEET_CACHE
    if _GROWN:
        have = {r["uuid"] for r in rows}
        rows += [r for r in _FLEET_CACHE if r["uuid"] not in have]
    else:
        _FLEET_CACHE = rows
    # anything blocked on a human answer outranks everything else
    rows.sort(key=lambda r: (0 if r.get("prompt") else 1,
                             {"working": 0, "idle": 1, "ended": 2}.get(r["state"], 3),
                             r["name"]))
    return rows, fleet_limits(files)


# ── session summary (headless `claude -p`) ─────────────────────────────────
# Two header lines for the chat view: a rolling summary of the whole session,
# and — while the task is still running — the condition that will make it stop.
# Generated by shelling out to `claude -p` (the local subscription, no API key).
#
# Computed when a chat is opened, not when the eye button is tapped (GET
# /api/summary is fired by both) — the `claude -p` call takes a couple of
# seconds, so warming it on open is what makes the brief instant when the eye
# asks for it. Never on a timer or for panes nobody opened. The result is cached
# keyed on the transcript's prompt count and reused for the whole run, so repeat
# taps are free and only a new prompt (a new count) pays for a fresh
# `claude -p --model haiku` call. Results persist to disk across restarts.
_SUMMARY_FILE = HERE / ".summaries.json"
_summary_locks = {}          # uuid -> asyncio.Lock (one claude call per pane)


def _load_summaries():
    try:
        return json.loads(_SUMMARY_FILE.read_text())
    except Exception:
        return {}


_summaries = _load_summaries()   # uuid -> {"prompts","summary","success","at"}


def _save_summaries():
    try:
        _SUMMARY_FILE.write_text(json.dumps(_summaries))
        _SUMMARY_FILE.chmod(0o600)
    except Exception as e:
        print(f"  [summary] save failed: {type(e).__name__}: {e}", flush=True)


def _content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return " ".join(
        c.get("text", "") for c in content
        if isinstance(c, dict) and (
            c.get("type") in ("text", "input_text", "output_text")
            or str(c.get("type") or "").endswith("_text")))


def _digest_turn(o):
    """One (role, text) pair from a Claude, Codex or Grok journal line, or None."""
    pl = o.get("payload")
    if isinstance(pl, dict):
        if pl.get("type") != "message":
            return None
        role, content = pl.get("role"), pl.get("content")
    elif o.get("message"):
        msg = o["message"]
        role, content = msg.get("role") or o.get("type"), msg.get("content")
    elif o.get("type") in ("user", "assistant"):
        role, content = o.get("type"), o.get("content")
    else:
        return None
    if role in ("system", "developer") or role not in ("user", "assistant"):
        return None
    text = _content_text(content)
    q = _USER_QUERY.search(text or "")
    if q:
        text = q.group(1).strip()
    elif role == "user":
        text = human_prompt(text)
        if text.startswith(("<user_info>", "<git_status>")):
            return None
    else:
        text = (text or "").strip()
        if any(n in text for n in _PROMPT_NOISE):
            return None
    if not text:
        return None
    return f"{role}: {text}"


def _transcript_digest(path, max_chars=20000):
    """Compact text of a session for summarisation: the opening user prompt (the
    task) plus the tail of the conversation, so both 'what it set out to do' and
    'what it's doing now' survive the truncation."""
    try:
        raw = pathlib.Path(path).read_text(errors="replace").splitlines()
    except Exception:
        return ""
    msgs = []
    for ln in raw:
        try:
            o = json.loads(ln)
        except Exception:
            continue
        turn = _digest_turn(o)
        if turn:
            msgs.append(turn)
    if not msgs:
        return ""
    first = msgs[0]
    tail = "\n".join(msgs[1:])
    if len(tail) > max_chars:
        tail = "…" + tail[-max_chars:]
    return (first + "\n" + tail)[:max_chars + 2000]


# Where each client installs itself, checked in order after PATH. The server is
# started by launchd/iTerm2 with a minimal PATH, so a bare binary name raises
# FileNotFoundError — look it up on PATH first, then these, and cache the answer.
BIN_PATHS = {
    "claude": ("~/.npm-packages/bin/claude", "~/.claude/local/claude",
               "~/.local/bin/claude", "/opt/homebrew/bin/claude",
               "/usr/local/bin/claude"),
    "codex":  ("~/.npm-packages/bin/codex", "~/.codex/bin/codex",
               "~/.local/bin/codex", "/opt/homebrew/bin/codex",
               "/usr/local/bin/codex"),
    "grok":   ("~/.grok/bin/grok", "~/.npm-packages/bin/grok",
               "~/.local/bin/grok", "/opt/homebrew/bin/grok",
               "/usr/local/bin/grok"),
}


def agent_bin(provider=DEFAULT_PROVIDER):
    """Absolute path to one client's CLI, cached per provider."""
    provider = provider if provider in PROVIDERS else DEFAULT_PROVIDER
    hit = _BINS.get(provider)
    if hit is not None:
        return hit
    found = shutil.which(provider)
    if not found:
        for c in BIN_PATHS[provider]:
            c = os.path.expanduser(c)
            if os.access(c, os.X_OK):
                found = c
                break
    _BINS[provider] = found or provider
    return _BINS[provider]


def _claude_bin():
    """The `claude` CLI specifically — the brief always runs on Claude."""
    return agent_bin(DEFAULT_PROVIDER)


_BINS = {}


async def _provider_summary(provider, digest, running, cwd=None):
    """Ask the selected native CLI for the two-line brief."""
    ask = (
        "You are labeling a coding session for a phone status bar. "
        "Below is a transcript digest (first prompt, then recent messages). "
        "Reply with EXACTLY two lines and nothing else:\n"
        "SUMMARY: <one sentence, <=110 chars, what this session has been doing overall>\n"
        "SUCCESS: <" + (
            "one sentence, <=110 chars, the concrete condition that will make the "
            "current task stop/finish>" if running else "the word NONE") + "\n\n"
        "Transcript digest:\n" + digest)
    try:
        if provider == "codex":
            # `exec` is Codex's non-interactive mode; ephemeral prevents this
            # labeling request from creating another saved session.
            cmd = [agent_bin("codex"), "exec", "--ephemeral", "-s", "read-only", ask]
        elif provider == "grok":
            # Grok's `--single` is its non-interactive equivalent.
            cmd = [agent_bin("grok"), "--single", ask, "--permission-mode", "plan"]
        else:
            # --model haiku keeps Claude labels cheap and fast.
            cmd = [_claude_bin(), "-p", "--model", "haiku", ask]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd if cwd and os.path.isdir(cwd) else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
    except Exception as e:
        print(f"  [summary] {provider} summary failed: {type(e).__name__}: {e}", flush=True)
        return None, None
    text = out.decode(errors="replace")
    summary, success = None, None
    for line in text.splitlines():
        s = line.strip()
        if s.upper().startswith("SUMMARY:"):
            summary = s.split(":", 1)[1].strip()[:140]
        elif s.upper().startswith("SUCCESS:"):
            v = s.split(":", 1)[1].strip()
            success = None if v.upper() in ("NONE", "N/A", "") else v[:140]
    return summary, success


async def _ensure_summary(uuid, path, pcount, running, provider=DEFAULT_PROVIDER, cwd=None):
    """Return this run's stored summary, computing it once if the prompt count
    moved (a new prompt = a new run to describe). One claude call per pane."""
    entry = _summaries.get(uuid)
    if entry and entry.get("prompts") == pcount:
        return entry
    lock = _summary_locks.setdefault(uuid, asyncio.Lock())
    async with lock:
        entry = _summaries.get(uuid)          # recheck after waiting on the lock
        if entry and entry.get("prompts") == pcount:
            return entry
        digest = await asyncio.to_thread(_transcript_digest, path)
        if not digest:
            return entry
        summary, success = await _provider_summary(provider, digest, running, cwd)
        if summary is None and success is None:
            # The claude call itself failed (timeout, not installed). Don't cache
            # the blank — it would stick for the whole run; leave the slot stale
            # so the next open retries.
            return entry
        entry = {"prompts": pcount, "summary": summary, "success": success,
                 "at": time.time()}
        _summaries[uuid] = entry
        await asyncio.to_thread(_save_summaries)
        print(f"  [summary] {uuid[:8]} summarised at prompt #{pcount}", flush=True)
        return entry


async def api_summary(request):
    if not authed(request):
        return web.json_response({"error": "locked"}, status=401)
    uuid = (request.query.get("uuid") or "").upper()
    f = read_fleet_files().get(uuid) or {}
    path = f.get("transcript")
    running = f.get("state") == "working"
    if not path or not os.path.exists(path):
        # Pane gone — still surface the last summary we saved for it, if any.
        e = _summaries.get(uuid) or {}
        return web.json_response({"summary": e.get("summary"),
                                  "success": e.get("success"), "running": False})
    provider = f.get("provider") or DEFAULT_PROVIDER
    pcount = (session_ops(path)[1] if provider == DEFAULT_PROVIDER
              else native_ops(provider, path)["prompts"])
    entry = await _ensure_summary(uuid, path, pcount, running, provider,
                                  f.get("cwd")) or {}
    return web.json_response({"summary": entry.get("summary"),
                              "success": entry.get("success"),
                              "running": running, "prompts": pcount})


# ── auth ───────────────────────────────────────────────────────────────────
# The token is a BOOTSTRAP credential only: it is accepted once, at "/", and
# immediately exchanged for an HttpOnly session cookie. No API or socket ever
# looks at it, so it cannot be replayed from a URL, a screenshot or a log.
# A machine woken for Dispatch is pinned awake with `pmset disablesleep`, and
# something has to decide when nobody is using it any more and let it sleep.
# The autosleep watchdog on that machine reads this file's mtime, and every
# authenticated request is evidence of use. Throttled hard: this sits on the
# request path, and a stamp a minute is all the watchdog needs.
_ACT_LAST = 0.0


def note_activity():
    global _ACT_LAST
    now = time.time()
    if now - _ACT_LAST < 60:
        return
    _ACT_LAST = now
    try:
        ACTIVITY_FILE.touch()
    except OSError:
        pass


def authed(request):
    ok = auth.unlocked(request) is not None
    if ok:
        note_activity()
    return ok


def guard(handler):
    async def wrapped(request):
        if not auth.same_origin(request):
            auth.audit(request, "csrf.block", {"origin": request.headers.get("Origin")})
            return web.json_response({"error": "bad origin"}, status=403)
        if auth.unlocked(request) is None:
            s = auth.get_session(request, touch=False)
            return web.json_response(
                {"error": "locked" if s else "no session",
                 "relock": bool(s)}, status=401)
        note_activity()
        return await handler(request)
    return wrapped


_PANE_WRITES = set()


def writes(action):
    """Wrap a state-changing endpoint: audit it, and never let it run locked."""
    def deco(handler):
        async def wrapped(request):
            if not auth.same_origin(request):
                auth.audit(request, "csrf.block",
                           {"origin": request.headers.get("Origin"), "for": action})
                return web.json_response({"error": "bad origin"}, status=403)
            if auth.unlocked(request) is None:
                s = auth.get_session(request, touch=False)
                return web.json_response({"error": "locked", "relock": bool(s)},
                                         status=401)
            body = {}
            try:
                body = await request.json()
            except Exception:
                pass
            request["_body"] = body
            auth.audit(request, action, {k: str(v)[:120] for k, v in body.items()})
            uuid = str(body.get("uuid", "")).upper()
            serial = uuid and action in {"key", "send", "cmd", "model", "effort", "mode", "prompt"}
            if serial and uuid in _PANE_WRITES:
                return web.json_response({"error": "pane control is busy; try again"}, status=409)
            if serial:
                _PANE_WRITES.add(uuid)
            try:
                return await handler(request)
            finally:
                if serial:
                    _PANE_WRITES.discard(uuid)
        return wrapped
    return deco


# ── routes ─────────────────────────────────────────────────────────────────
async def client_log(request):
    """A phone has no console you can open. Errors come here instead."""
    try:
        d = await request.json()
    except Exception:
        d = {}
    print(f"  [client] {auth.client_ip(request)}: {str(d.get('msg'))[:400]}",
          flush=True)
    return web.json_response({"ok": True})



# The icon and manifest are the only unauthenticated responses in the app. They
# have to be: a launcher fetches them with no cookie when the icon is installed,
# and they reveal nothing but a logo.
async def manifest(request):
    return web.json_response({
        "name": "The Yard", "short_name": "Yard",
        "start_url": "/", "scope": "/",
        "display": "standalone", "orientation": "portrait",
        "background_color": "#14171b", "theme_color": "#14171b",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/icons/icon-maskable-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }, headers={"Cache-Control": "public, max-age=3600"})


async def icon(request):
    name = request.match_info["name"]
    if not re.fullmatch(r"icon-(maskable-)?\d{3}\.png", name):
        return web.Response(status=404)
    path = HERE / "static" / "icons" / name
    if not path.exists():
        return web.Response(status=404)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})



async def index(request):
    """Bootstrap, then serve the shell.

    A `?t=` token in the URL (the QR) is spent here exactly once: it mints a
    session cookie and we redirect to a clean "/" so the secret never sits in
    the address bar, history or a screenshot. After that the cookie — plus a
    passkey — is the only way in.
    """
    ip = auth.client_ip(request)
    left = auth.locked_out(ip)
    if left:
        return web.Response(status=429, text=f"locked out, retry in {left}s",
                            headers={"Retry-After": str(left)})

    tok = request.query.get("t")
    if tok is not None and auth.get_session(request) is None:
        if not secrets.compare_digest(tok, TOKEN):
            auth.note_fail(ip)
            auth.audit(request, "bootstrap.fail", {"len": len(tok)})
            print(f"  [index] {ip} REJECTED — bad bootstrap token", flush=True)
            return web.Response(status=401, text="401 — bad token")
        sid = auth.new_session(ip)
        auth.audit(request, "bootstrap.ok")
        resp = web.HTTPFound("/")                       # drop ?t= from the bar
        auth.set_session_cookie(resp, sid, request)
        print(f"  [index] {ip} bootstrapped a session", flush=True)
        return resp

    if tok is not None:                                 # already had a session
        resp = web.HTTPFound("/")
        return resp

    # No session cookie. If a passkey is already registered for this origin,
    # serve the shell anyway — its gate runs a passkey unlock that mints a fresh
    # session (see auth.login_begin), so a lapsed session no longer forces a
    # token paste. Only fall back to the token gate when there's no passkey to
    # unlock with, i.e. a brand-new browser that must bootstrap to enrol one.
    have_passkey = (auth.passkey_capable(request)
                    and auth.has_passkey(auth.rp_id(request)))
    if auth.get_session(request) is None and not have_passkey:
        auth.audit(request, "index.nosession")
        return web.Response(
            status=401, content_type="text/html",
            headers={"Cache-Control": "no-store"},
            text="""<meta name=viewport content="width=device-width,initial-scale=1">
<body style="background:#14171b;color:#f2f5f9;font:15px/1.6 -apple-system,
 BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:34px 22px;margin:0">
<h2 style="font:700 14px/1 sans-serif;letter-spacing:.16em;text-transform:uppercase;
 color:#d77757;margin:0 0 14px">The Yard — locked</h2>
<p style="color:#a8b2bf;margin:0 0 18px">No session in this browser. Paste the
 token printed by the server to start one.</p>
<input id=t placeholder="token" autocomplete="off" autocapitalize="none"
 spellcheck="false" style="width:100%;box-sizing:border-box;background:#22262d;
 border:1px solid #434b58;border-radius:6px;color:#f2f5f9;padding:13px;
 font:14px ui-monospace,monospace">
<button onclick="go()" style="margin-top:12px;width:100%;background:#d77757;
 border:none;border-radius:6px;color:#2a1206;font:700 15px sans-serif;
 padding:14px;cursor:pointer">Start session</button>
<div id=e style="color:#ff6b61;font:12px ui-monospace,monospace;margin-top:12px"></div>
<p style="color:#78828f;font-size:12px;margin-top:22px">Scanning the QR in an
 app's built-in browser starts the session there, not in Chrome. Open the link
 in your real browser, or paste the token here.</p>
<script>
async function go(){
  const r = await fetch('/auth/bootstrap', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({token: document.getElementById('t').value.trim()})});
  const d = await r.json().catch(()=>({}));
  if (r.ok) location.replace('/');
  else document.getElementById('e').textContent = d.error || r.status;
}
document.getElementById('t').addEventListener('keydown', e => {
  if (e.key === 'Enter') go();
});
</script></body>""")

    body = (HERE / "static" / "index.html").read_text()
    resp = web.Response(text=body, content_type="text/html")
    # served straight off disk and edited often — never let the phone cache it
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    if authed(request):
        resp.set_cookie("t", TOKEN, max_age=60 * 60 * 24 * 365,
                        samesite="Lax", httponly=False)
    return resp


@guard
async def api_fleet(request):
    rows, limits = await build_fleet()
    return web.json_response({"sessions": rows, "limits": limits})


@writes("key")
async def api_key(request):
    body = await request.json()
    uuid, k = body.get("uuid", "").upper(), body.get("key")
    if k not in KEYS:
        return web.json_response({"error": f"unknown key {k!r}"}, status=400)
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    # never type into a plain shell by accident — but identify the pane by its
    # Claude UI, not by jobName, which is often a child (caffeinate, bash, git)
    if not is_agent_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no agent UI — refusing"},
            status=403)
    await s.async_send_text(KEYS[k])
    return web.json_response({"ok": True, "sent": k})


@writes("key")
async def api_select(request):
    """Drive a checkbox multi-select: walk the caret to each row that needs
    toggling and space it, then optionally submit. Always re-parses the pane
    fresh — the client's view of which rows are checked and where the caret
    sits can be a frame stale by the time this lands, and getting that wrong
    means toggling the wrong option.
    """
    body = await request.json()
    uuid = body.get("uuid", "").upper()
    indices = set(body.get("indices") or [])
    submit = bool(body.get("submit", True))
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    text = await pane_text(s)
    if not is_agent_pane(uuid, job, text):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no agent UI — refusing"},
            status=403)
    p = detect_prompt(text)
    if not p or not p["multi"]:
        return web.json_response(
            {"error": "no multi-select prompt on screen"}, status=400)

    opts = p["options"]
    caret = next((o["index"] for o in opts if o["selected"]), 0)
    toggled = 0
    for o in sorted(opts, key=lambda o: o["index"]):
        want = o["index"] in indices
        if want == o["checked"]:
            continue
        while caret < o["index"]:
            await s.async_send_text(KEYS["down"])
            await asyncio.sleep(0.04)
            caret += 1
        while caret > o["index"]:
            await s.async_send_text(KEYS["up"])
            await asyncio.sleep(0.04)
            caret -= 1
        await s.async_send_text(KEYS["space"])
        await asyncio.sleep(0.04)
        toggled += 1
    if submit:
        await s.async_send_text(KEYS["enter"])
    return web.json_response({"ok": True, "toggled": toggled, "submitted": submit})


@writes("send")
async def api_send(request):
    body = await request.json()
    uuid = body.get("uuid", "").upper()
    text = body.get("text", "")
    submit = bool(body.get("submit", True))
    if not text.strip():
        return web.json_response({"error": "empty"}, status=400)
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    screen = await pane_text(s)
    provider = pane_provider(uuid, job, screen)
    if not provider:
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no agent UI — refusing"},
            status=403)
    if provider == "codex":
        if detect_prompt(screen, provider, uuid):
            return web.json_response({"error": "answer the active Codex prompt first"}, status=409)
        try:
            text = await _send_provider_text(s, text, provider, submit)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
    else:
        text = await _send_provider_text(s, text, provider, submit)
    return web.json_response({"ok": True, "chars": len(text)})


def _codex_safe_text(text):
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any((ord(c) < 32 and c != "\n") or 127 <= ord(c) <= 159 for c in text):
        raise ValueError("text contains terminal control characters")
    return text


async def _send_provider_text(sess, text, provider, submit=True):
    """Type literal text using the target client's safe paste semantics."""
    if provider == "codex":
        text = _codex_safe_text(text)
        await sess.async_send_text("\x1b[200~" + text + "\x1b[201~")
    else:
        await sess.async_send_text(text)
    if submit:
        await asyncio.sleep(0.15)
        await sess.async_send_text("\r")
    return text


async def _prompt_wait(s, uuid, predicate):
    for _ in range(20):
        prompt = detect_prompt(await pane_text(s), "codex", uuid)
        if predicate(prompt):
            return prompt
        await asyncio.sleep(0.15)
    raise RuntimeError("Codex prompt did not change as expected; refresh and try again")


async def _codex_prompt_action(s, uuid, prompt, action, option_index=None, text=None):
    identity = prompt["id"]

    def same(p):
        return p is not None and p.get("id") == identity

    async def stable():
        fresh = detect_prompt(await pane_text(s), "codex", uuid)
        if not same(fresh):
            raise RuntimeError("Codex prompt changed; refresh before answering")
        return fresh

    if action in ("previous", "next"):
        await s.async_send_text("\x10" if action == "previous" else "\x0e")
        wanted = prompt["question_index"] + (-1 if action == "previous" else 1)
        return await _prompt_wait(s, uuid, lambda p: p is not None and p.get("question_index") == wanted)
    if action == "cancel":
        for attempt in range(2):
            await s.async_send_text("\x03" if prompt["kind"] == "question" else "\x1b")
            try:
                return await _prompt_wait(s, uuid, lambda p: not same(p))
            except RuntimeError:
                if attempt or prompt["kind"] != "question":
                    raise
                await stable()  # first Ctrl-C may only clear the notes composer
        raise RuntimeError("Codex prompt did not cancel")

    if text is None:
        text = prompt["input"]["text"]
    # Tab clears notes and restores the option caret. Preserve the requested
    # draft locally, then replace it after navigating the selected option.
    if prompt["options"] and prompt["input"]["visible"]:
        await s.async_send_text("\t")
        prompt = await _prompt_wait(s, uuid, lambda p: same(p) and not p["input"]["visible"])
    if prompt["options"]:
        selected = next((o["index"] for o in prompt["options"] if o["selected"]), None)
        if selected is None:
            raise RuntimeError("Codex option caret is not visible")
        target = selected if option_index is None else option_index
        for _ in range(len(prompt["options"])):
            if selected == target:
                break
            step = 1 if target > selected else -1
            await stable()
            await s.async_send_text("\x1b[B" if step > 0 else "\x1b[A")
            selected += step
            prompt = await _prompt_wait(s, uuid, lambda p: same(p) and any(
                o["index"] == selected and o["selected"] for o in p["options"]))
        label = prompt["options"][target]["label"].lower()
        if action == "submit" and (text or label.startswith("none of the above")):
            await s.async_send_text("\t")
            prompt = await _prompt_wait(s, uuid, lambda p: same(p) and p["input"]["visible"])
    elif prompt["input"]["text"]:
        await s.async_send_text("\x03")
        prompt = await _prompt_wait(s, uuid, lambda p: same(p) and p["input"]["visible"] and not p["input"]["text"])
    if action == "submit" and text:
        await stable()
        await s.async_send_text("\x1b[200~" + text + "\x1b[201~")
        normalized = " ".join(text.split())
        await _prompt_wait(s, uuid, lambda p: same(p) and " ".join(p["input"]["text"].split()) == normalized)
    await stable()
    await s.async_send_text("\r")
    return await _prompt_wait(s, uuid, lambda p: not same(p))


@writes("prompt")
async def api_prompt(request):
    body = await request.json()
    uuid = str(body.get("uuid", "")).upper()
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    screen = await pane_text(s)
    provider = pane_provider(uuid, await s.async_get_variable("jobName") or "", screen)
    if provider not in ("codex", "grok"):
        return web.json_response(
            {"error": "this prompt control requires a Codex or Grok pane"}, status=403)
    prompt = detect_prompt(screen, provider, uuid)
    if not prompt or prompt.get("id") != body.get("prompt_id"):
        return web.json_response(
            {"error": f"{provider.capitalize()} prompt is stale; refresh before answering"},
            status=409)
    action = body.get("action")
    if action not in prompt["actions"]:
        return web.json_response({"error": "action is not available for this prompt"}, status=400)
    index = body.get("option_index")
    if index is not None and (type(index) is not int or not 0 <= index < len(prompt["options"])):
        return web.json_response({"error": "invalid option index"}, status=400)
    if action == "choose" and index is None:
        return web.json_response({"error": "option index is required"}, status=400)
    if provider == "grok":
        if action not in ("choose", "cancel"):
            return web.json_response({"error": "action is not available for this prompt"}, status=400)
        try:
            fresh = await _grok_prompt_action(s, uuid, prompt, action, index)
            return web.json_response({"ok": True, "prompt": fresh})
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=409)
    try:
        text = _codex_safe_text(body["text"]) if "text" in body else None
        if text and (action != "submit" or not prompt["input"]["allowed"]):
            raise ValueError("text is not supported for this action")
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    try:
        fresh = await _codex_prompt_action(s, uuid, prompt, action, index, text)
        return web.json_response({"ok": True, "prompt": fresh})
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=409)


# ── file upload (image / video from the phone) ──────────────────────────────
# Saved to the pane's working dir under .dispatch-uploads/ so Claude can Read it
# by the absolute path we hand back. Falls back to a shared uploads dir if the
# pane's cwd can't be resolved.
_UPLOAD_MAX = 200 * 1024 * 1024        # 200 MB — videos are big
_UPLOAD_FALLBACK = HERE / "uploads"


def _safe_name(name):
    name = os.path.basename(name or "file")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or "file"
    return name[:120]


def _mb(n):
    return f"{n / (1024 * 1024):.1f} MB"


@guard
async def api_upload(request):
    uuid = (request.headers.get("X-Uuid") or "").upper()
    fname = _safe_name(request.headers.get("X-Filename") or "file")
    # Refuse on the declared length before reading a byte, so an oversized file
    # is answered immediately instead of after a phone has spent a minute
    # uploading it over a tailnet.
    declared = request.content_length or 0
    if declared > _UPLOAD_MAX:
        return web.json_response(
            {"error": f"file is {_mb(declared)} — the limit is {_mb(_UPLOAD_MAX)}"},
            status=413)
    # target dir: the pane's live working dir, else the fallback
    dest_dir = None
    s = (await all_sessions()).get(uuid)
    if s:
        f = (await build_fleet())[0]
        row = next((r for r in f if r["uuid"] == uuid), None)
        cwd = (row or {}).get("cwd")
        if cwd and os.path.isdir(cwd):
            dest_dir = os.path.join(cwd, ".dispatch-uploads")
    if dest_dir is None:
        dest_dir = str(_UPLOAD_FALLBACK)
    path = None
    written = 0
    try:
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, fname)
        # never clobber: add -1, -2 … if the name exists
        stem, ext = os.path.splitext(fname)
        n = 1
        while os.path.exists(path):
            path = os.path.join(dest_dir, f"{stem}-{n}{ext}"); n += 1
        # Stream it. A 4K video is hundreds of megabytes; reading that into a
        # bytes object first would hold the whole thing in the server's memory
        # for no reason, and a body that lied about its length would get past
        # the check above.
        with open(path, "wb") as fh:
            async for chunk in request.content.iter_chunked(256 * 1024):
                written += len(chunk)
                if written > _UPLOAD_MAX:
                    fh.close()
                    os.unlink(path)
                    return web.json_response(
                        {"error": f"file exceeds the {_mb(_UPLOAD_MAX)} limit"},
                        status=413)
                await asyncio.to_thread(fh.write, chunk)
    except Exception as e:
        if path and os.path.exists(path) and not written:
            try:
                os.unlink(path)            # never leave a 0-byte stub behind
            except OSError:
                pass
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)
    if not written:
        try:
            os.unlink(path)
        except OSError:
            pass
        return web.json_response({"error": "no file"}, status=400)
    auth.audit(request, "upload", {"path": path, "bytes": written})
    print(f"  [upload] {written}B → {path}", flush=True)
    return web.json_response({"ok": True, "path": path, "name": os.path.basename(path),
                              "bytes": written})


# ── voice → text (local whisper.cpp) ───────────────────────────────────────
# All local: the browser records audio, we ffmpeg it to 16k mono wav and run
# whisper-cli. Nothing leaves the machine. Model is downloaded once to models/.
# Resolve the media tools by hand: a nohup/launchd start inherits a bare PATH
# without /opt/homebrew/bin, so a plain "whisper-cli"/"ffmpeg" would not be found
# even when installed. Same reasoning as _ts_bin above.
def _find_bin(name):
    import shutil
    onpath = shutil.which(name)
    if onpath:
        return onpath
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand
    return name


WHISPER_BIN = os.environ.get("WHISPER_BIN") or _find_bin("whisper-cli")
WHISPER_MODEL = os.environ.get(
    "WHISPER_MODEL", str(HERE / "models" / "ggml-base.en.bin"))
FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or _find_bin("ffmpeg")
WHISPER_MAX_BYTES = 25 * 1024 * 1024      # ~25 MB of recorded audio is plenty


def _transcribe(raw):
    """Blocking: browser blob → wav → text. Runs in a worker thread."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "clip")
        wav = os.path.join(td, "clip.wav")
        with open(src, "wb") as f:
            f.write(raw)
        # The browser hands us webm/opus (Android/desktop) or mp4/aac (iOS);
        # whisper-cli only reads wav/mp3/flac/ogg, so normalise everything.
        subprocess.run(
            [FFMPEG_BIN, "-nostdin", "-y", "-i", src,
             "-ar", "16000", "-ac", "1", "-f", "wav", wav],
            check=True, capture_output=True, timeout=60)
        out = subprocess.run(
            [WHISPER_BIN, "-m", WHISPER_MODEL, "-nt", "-np", "-f", wav],
            check=True, capture_output=True, timeout=120, text=True)
        # -nt/-np keep stdout to just the transcript; backend logs go to stderr
        return out.stdout.strip()


@guard
async def api_whisper(request):
    raw = await request.read()
    if not raw:
        return web.json_response({"error": "no audio"}, status=400)
    if len(raw) > WHISPER_MAX_BYTES:
        return web.json_response({"error": "audio too large"}, status=413)
    if not os.path.exists(WHISPER_MODEL):
        return web.json_response(
            {"error": "whisper model missing on server"}, status=503)
    auth.audit(request, "whisper", {"bytes": len(raw)})
    try:
        text = await asyncio.to_thread(_transcribe, raw)
    except subprocess.TimeoutExpired:
        return web.json_response({"error": "transcription timed out"}, status=504)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b"")
        if isinstance(tail, bytes):
            tail = tail.decode(errors="replace")
        return web.json_response(
            {"error": "transcription failed", "detail": tail[-200:]}, status=500)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True, "text": text})


# Claude's input line: a `❯` prompt. A ghost auto-suggestion is rendered with a
# NON-breaking space after the caret (❯\xa0…); text the user actually typed uses
# a regular space (❯ …). We key off that to tell "accept Claude's suggestion"
# apart from "submit what's already typed".
_PROMPT_CARET = "❯"


def _input_suggestion(text):
    """(kind, suggestion) for the pane's input line.

    kind: "ghost" with the suggested text, "typed" if there's real typed text,
    or "empty". Ghosts can't be committed by a keystroke over the API (Tab/→ do
    nothing), so the caller retypes the suggestion as a real prompt instead —
    which is why it has to be the WHOLE suggestion, wrapped rows included, or we
    submit a command cut off at the pane's width. read_input_box does that.
    """
    ghost, s = read_input_box(text)
    if not s:
        return "empty", ""
    return ("ghost" if ghost else "typed"), s


@writes("send")
async def api_submit(request):
    """"Just send it" — the phone's ▶ with an empty box.

    If Claude is showing a ghost-suggested prompt, retype it and submit (a bare
    Enter won't: the suggestion isn't in the buffer and no accept-key commits it
    through the API). Otherwise just press Enter to submit whatever is typed.
    """
    body = await request.json()
    uuid = body.get("uuid", "").upper()
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    txt = await pane_text(s)
    if not is_agent_pane(uuid, job, txt):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no agent UI — refusing"},
            status=403)
    kind, sug = _input_suggestion(txt)
    if kind == "ghost" and sug:
        await s.async_send_text(sug)          # retype the suggestion as real input
        await asyncio.sleep(0.15)
    await s.async_send_text("\r")             # submit (typed text, or the retype)
    return web.json_response({"ok": True, "kind": kind, "sent": sug})


async def _auto_trust(sess):
    """Auto-answer Claude's first-run "Do you trust the files in this folder?".

    trust_dir() pre-writes hasTrustDialogAccepted so the prompt normally never
    fires, but a concurrent ~/.claude.json rewrite by another Claude can drop
    that fresh key before this pane reads it, and then the pane sits blocked on
    the trust dialog. This is the belt-and-suspenders: watch the new pane for a
    few seconds and, if the trust prompt appears, pick its "Yes, proceed" option.
    Scoped hard to the trust dialog — any other prompt is left untouched.
    """
    deadline = time.time() + 15
    while time.time() < deadline:
        await asyncio.sleep(0.6)
        try:
            text = await pane_text(sess)
        except Exception:
            continue
        if "trust the files in this folder" not in text.lower():
            continue                      # only ever act on the trust dialog
        p = detect_prompt(text)
        if not p:
            continue
        yes = next((o for o in p["options"]
                    if any(w in o["label"].lower()
                           for w in ("yes", "proceed", "trust"))), None)
        if not yes:
            return
        await sess.async_send_text(KEYS[yes["key"]])
        print(f"  [spawn] auto-accepted trust prompt (option {yes['key']})",
              flush=True)
        return


# ── how a pane is born ──────────────────────────────────────────────────────
# A pane runs a launcher script as its own process instead of a login shell that
# we then type a command into. Typing into a login shell is what put the noise at
# the top of every phone transcript: `login` prints "Last login: ...", an
# interactive bash prints Apple's zsh-deprecation notice, the typed line is echoed
# once as type-ahead and again by the prompt that finally reads it, and `clear`
# only wipes the visible screen -- all of it stays in the scrollback the phone
# reads. Hosts differed only in how loud their shell was (bash and no ~/.hushlogin
# on Big Mac, a quiet zsh on the MacBook), never in what Dispatch did. Booting the
# pane straight into the launcher makes Claude's first frame the pane's first line
# on every host.


def _pane_launcher(workdir, env_lines=(), args="", provider=DEFAULT_PROVIDER):
    """Write a self-deleting launcher for a new pane; return (command, tmpdir).

    Secrets ride in the file (0600, unlinked before the client starts), never as
    keystrokes, so they cannot land in the pane's scrollback or shell history --
    the same contract the old .cc-inject.env had, minus the dotfile dropped into
    the owner's own repo. `bash -l` on a *script* reads the login profile (so the
    pane keeps the owner's PATH) without being interactive (so it prints nothing).
    """
    d = tempfile.mkdtemp(prefix="cc-launch-")
    path = os.path.join(d, "launch.sh")
    body = [
        "# CC Dispatch pane launcher. Deletes itself before the agent takes the pane.",
        "export BASH_SILENCE_DEPRECATION_WARNING=1",
        *env_lines,
        f"cd {shlex.quote(workdir)} || exit 1",
        f"rm -f {shlex.quote(path)}; rmdir {shlex.quote(d)} 2>/dev/null",
        f"{shlex.quote(agent_bin(provider))} {args}".rstrip(),
        "exec /bin/bash -il",          # agent exited: leave the live shell it used to
    ]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, ("\n".join(body) + "\n").encode())
    os.close(fd)
    return f"/bin/bash -l {path}", d


def _launch_profile(cmd):
    """Profile override that starts a split on `cmd` rather than on a login shell."""
    chg = iterm2.LocalWriteOnlyProfile()
    chg.set_use_custom_command("Yes")
    chg.set_command(cmd)
    return chg


def _discard_launcher(d):
    """Drop an unused launcher (and the secrets in it) when the split never opened."""
    shutil.rmtree(d, ignore_errors=True)


INITIAL_PROMPT_MAX = 2000


def _spawn_options(body):
    """Validate spawn inputs, preserving the requested agent for prompted launches."""
    provider = (body.get("provider") or DEFAULT_PROVIDER).strip().lower()
    prompt = body.get("initial_prompt")
    if prompt is not None:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("initial prompt must be non-empty text")
        prompt = prompt.strip()
        if len(prompt) > INITIAL_PROMPT_MAX:
            raise ValueError(f"initial prompt exceeds {INITIAL_PROMPT_MAX} characters")
        return provider, prompt
    return provider, None


async def _deliver_initial_prompt(sess, prompt, provider=DEFAULT_PROVIDER,
                                  timeout=30):
    """Wait for the launched agent's UI, then type and submit its first task."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(0.5)
        try:
            text = await pane_text(sess)
        except Exception:
            continue
        if marker_provider(text) != provider:
            continue
        if provider == "codex" and detect_prompt(
                text, provider, sess.session_id.upper()):
            continue
        try:
            await _send_provider_text(sess, prompt, provider)
        except ValueError:
            return False
        print(f"  [spawn] delivered initial prompt to {sess.session_id.upper()}",
              flush=True)
        return True
    print(f"  [spawn] timed out delivering initial prompt to "
          f"{sess.session_id.upper()}", flush=True)
    return False


@writes("spawn")
async def api_spawn(request):
    """Open a brand-new agent pane in the iTerm window and hand back its UUID.

    `provider` picks which CLI the pane boots into — claude, codex or grok. It
    starts in a throwaway scratch dir so nothing real is touched until you tell it
    where to work. For Claude the scratch dir is pre-trusted in ~/.claude.json so
    the first-run "trust the files in this folder?" prompt never fires. We add the
    UUID to KNOWN_AGENTS up front, under the provider we launched, so it lands in
    the fleet the instant it opens — before its jobName has settled to `node` and
    before any of the marker heuristics have a screen to read.
    """
    await APP.async_refresh()
    body = request.get("_body") or {}
    try:
        provider, initial_prompt = _spawn_options(body)
    except (AttributeError, ValueError) as e:
        return web.json_response({"error": str(e)}, status=400)
    if provider not in PROVIDERS:
        return web.json_response(
            {"error": f"unknown agent {provider!r}"}, status=400)
    if not os.path.isabs(agent_bin(provider)):
        return web.json_response(
            {"error": f"{provider} is not installed on this host"}, status=409)
    # Chosen dir from the picker: cd straight there. Absent → throwaway scratch, so
    # nothing real is touched until the owner picks a folder. Either way the launch
    # dir is trusted up front so Claude's first-run trust prompt never fires.
    chosen = (body.get("dir") or "").strip()
    if chosen:
        chosen = os.path.abspath(os.path.expanduser(chosen))
        if not os.path.isdir(chosen):
            return web.json_response({"error": "not a directory"}, status=400)
        workdir = chosen
        _note_recent_dir(workdir)
    else:
        workdir = tempfile.mkdtemp(prefix="cc-scratch-")
    scratch = workdir
    is_scratch = not chosen
    if provider == DEFAULT_PROVIDER:
        trust_dir(scratch)                 # skip Claude's first-run trust prompt

    # Hand the pane a capability handle for the auth broker, plus any credentials
    # the owner chose to pre-inject. All of it goes into the launcher the pane runs
    # as its own process — a 0600 file it deletes before Claude starts — so the raw
    # values are NEVER keystroked and can't land in the pane's scrollback or shell
    # history. DISPATCH_AGENT_TOKEN + DISPATCH_URL are capability handles (not
    # secrets); pre-injected service tokens are secrets.
    tok = secrets.token_urlsafe(24)
    lines = [f'export PATH={shlex.quote(str(HERE))}:"$PATH"',
             f'export DISPATCH_URL="http://127.0.0.1:{PORT}"',
             f'export DISPATCH_AGENT_TOKEN={shlex.quote(tok)}']
    released = []
    for cid in (body.get("integrations") or []):
        cred = vault.get_cred(cid)
        if not cred:
            continue
        lines.append(f'export {cred["env_var"]}={shlex.quote(cred["secret"])}')
        released.append((cid, cred))
    # Teach the agent the protocol so it knows it can ask for what it lacks. Only
    # in a scratch dir — never drop a CLAUDE.md into a real repo the owner picked.
    if is_scratch:
        try:
            (pathlib.Path(scratch) / AGENT_NOTE_FILE[provider]).write_text(_AGENT_NOTE)
        except Exception:
            pass

    # Claude panes are born in bypass — it is the only way in (no Shift-Tab path,
    # no slash command), and a pane you drive from a phone can't answer prompts.
    args = "--dangerously-skip-permissions" if provider == DEFAULT_PROVIDER else ""
    cmd, launch_dir = _pane_launcher(scratch, lines, args=args, provider=provider)
    # Grow the fleet's own tab into a grid instead of opening a new tab. Panes are
    # placed row-major (see GRID_MAX_COLS) so the split lands in an aligned column
    # or row rather than as a random narrow sliver.
    try:
        tab = fleet_tab(APP)
        if tab is None:
            _discard_launcher(launch_dir)
            return web.json_response(
                {"error": "no iTerm window open to spawn into"}, status=409)
        src, vertical = pick_grid_split(tab)
        sess = await src.async_split_pane(vertical=vertical, before=False,
                                          profile_customizations=_launch_profile(cmd))
    except Exception as e:
        _discard_launcher(launch_dir)
        return web.json_response(
            {"error": f"could not open pane: {type(e).__name__}: {e}"}, status=500)
    uuid = sess.session_id.upper()
    KNOWN_AGENTS[uuid] = provider
    PANE_TOKENS[tok] = uuid
    for cid, cred in released:
        vault.add_grant(uuid, cid, scopes=cred.get("scopes") or [])   # visible + revocable
        auth.audit(request, "integ.release",
                   {"uuid": uuid, "cred": cid, "last4": cred["last4"], "via": "spawn"})
    await normalize_pane(sess)             # land at the canonical width from birth
    # Fallback in case the pre-trust key got clobbered — auto-accept the trust
    # dialog if it still shows. Claude's dialog only; the others do not ask.
    # Fire-and-forget so the spawn returns immediately.
    if provider == DEFAULT_PROVIDER:
        asyncio.create_task(_auto_trust(sess))
    if initial_prompt:
        asyncio.create_task(_deliver_initial_prompt(sess, initial_prompt, provider))
    print(f"  [spawn] new {provider} pane {uuid} in {scratch}", flush=True)
    return web.json_response({"uuid": uuid, "dir": scratch, "provider": provider})


_AGENT_NOTE = """\
# This pane is managed by CC Dispatch

## Getting credentials

You don't hold service credentials by default. When you need one (GitHub, Vercel,
a database URL, npm, a cloud key, …), ask the owner — it pings their phone:

    dispatch-auth request <service> [reason]

Once they approve it on their phone, load it just-in-time:

    export GITHUB_TOKEN=$(dispatch-auth get github)     # blocks until approved

`dispatch-auth list` shows what this pane already holds. Treat any secret you
receive as sensitive: use it, but never write it into a file you might commit.
"""


def _browse_roots():
    """Top-level entries the dir picker starts from: the owner's home, then any
    mounted volume (external drives, other Macs) under /Volumes. `HOME` first so
    "Charlie BC" (the home dir) is the default landing spot."""
    home = os.path.expanduser("~")
    roots = [{"name": os.path.basename(home.rstrip("/")) or home, "path": home}]
    try:
        for name in sorted(os.listdir("/Volumes")):
            p = os.path.join("/Volumes", name)
            if os.path.isdir(p) and not name.startswith("."):
                roots.append({"name": name, "path": p})
    except OSError:
        pass
    return roots


def installed_agents():
    """Which client CLIs this host actually has — the picker only offers these.
    An unresolved lookup falls back to the bare name, which is never absolute."""
    return [p for p in PROVIDERS if os.path.isabs(agent_bin(p))]


RECENTS_FILE = HERE / ".recent_dirs.json"
RECENTS_MAX = 12


def _load_recent_dirs():
    try:
        raw = json.loads(RECENTS_FILE.read_text())
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict) and r.get("path")]


def _note_recent_dir(path):
    """Remember a real working dir so the picker can surface it next time."""
    if not path:
        return
    path = os.path.abspath(os.path.expanduser(path))
    if _is_scratch(path) or not os.path.isdir(path):
        return
    rec = {"path": path, "name": os.path.basename(path.rstrip("/")) or path,
           "last": time.time()}
    items = [r for r in _load_recent_dirs() if r.get("path") != path]
    items.insert(0, rec)
    try:
        RECENTS_FILE.write_text(json.dumps(items[:RECENTS_MAX]))
    except OSError:
        pass


def _recent_dirs():
    """Dirs the picker should offer first — newest `last` wins, existing only.

    Merges the recents file, live fleet cwds, and cached history. When history
    has never been built we also unquote ~/.grok/sessions/* names so a project
    you were just in still appears without a full transcript scan.
    """
    from urllib.parse import unquote
    by_path = {}

    def take(path, last, name=None):
        if not path:
            return
        path = os.path.abspath(os.path.expanduser(path))
        if _is_scratch(path) or not os.path.isdir(path):
            return
        last = last or 0
        prev = by_path.get(path)
        if prev and prev["last"] >= last:
            return
        by_path[path] = {
            "name": name or os.path.basename(path.rstrip("/")) or path,
            "path": path, "last": last}

    for r in _load_recent_dirs():
        take(r.get("path"), r.get("last") or 0, r.get("name"))
    for rec in read_fleet_files().values():
        take(rec.get("cwd"), rec.get("mtime") or rec.get("state_since") or 0)
    hist = _history_result.get("data")
    if isinstance(hist, list):
        for e in hist:
            if isinstance(e, dict):
                take(e.get("cwd"), e.get("last") or 0)
    try:
        names = os.listdir(GROK_SESSIONS)
    except OSError:
        names = []
    for name in names:
        full = os.path.join(GROK_SESSIONS, name)
        if not os.path.isdir(full):
            continue
        try:
            mt = os.path.getmtime(full)
        except OSError:
            continue
        take(unquote(name), mt)
    return sorted(by_path.values(), key=lambda d: d["last"], reverse=True)[:RECENTS_MAX]


@guard
async def api_browse(request):
    """List directories under `path` so the phone can click through the filesystem
    and pick where a new pane starts. No path → the roots (home + volumes). Read
    only; never returns files, only sub-directories. `agents` rides along so the
    picker can offer the agent choice in the same sheet it picks the folder in."""
    path = request.query.get("path", "")
    if not path:
        return web.json_response({"path": "", "parent": None, "dirs": [],
                                  "roots": _browse_roots(),
                                  "agents": installed_agents(),
                                  "recents": _recent_dirs()})
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return web.json_response({"error": "not a directory"}, status=404)
    dirs = []
    try:
        for name in os.listdir(path):
            if name.startswith("."):
                continue                       # hide dotfiles/dirs
            full = os.path.join(path, name)
            try:
                if os.path.isdir(full):
                    dirs.append({"name": name, "path": full,
                                 "mtime": os.stat(full).st_mtime})
            except OSError:
                continue
        # most recently touched first — the project you were just in floats to
        # the top; ties (same second) fall back to name so the order is stable
        dirs.sort(key=lambda d: (-d["mtime"], d["name"].lower()))
    except OSError as e:
        return web.json_response({"error": str(e)}, status=403)
    parent = os.path.dirname(path.rstrip("/"))
    if parent == path or not parent:
        parent = None
    return web.json_response({"path": path, "parent": parent,
                              "roots": _browse_roots(), "dirs": dirs,
                              "agents": installed_agents(),
                              "recents": _recent_dirs()})


# How each client is asked to shut itself down before the pane is closed.
QUIT_CMD = {"claude": "/exit", "codex": "/quit", "grok": "/exit"}


@writes("kill")
async def api_kill(request):
    """End a session and close its iTerm pane — the drag-to-trash gesture.

    Ctrl-C first so the agent tears down its own child processes, then its own
    quit command so it saves the session the way a normal quit would, then close
    the pane. The close is what removes the split/tab from the Mac; without it the
    shell just returns to a prompt and the pane lingers.
    """
    d = await request.json()
    uuid = (d.get("uuid") or "").upper()
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    err = await kill_pane(uuid, s)
    if err:
        return web.json_response({"error": err}, status=500)
    return web.json_response({"ok": True, "uuid": uuid})


async def kill_pane(uuid, session):
    """Quit one agent and close its pane. Returns an error string, or None."""
    try:
        await session.async_send_text("\x03")    # interrupt whatever is running
        await asyncio.sleep(0.2)
        await session.async_send_text(QUIT_CMD.get(provider_of(uuid), "/exit") + "\r")
        await asyncio.sleep(0.6)
    except Exception as e:
        print(f"  [kill] {uuid} graceful stop failed: {type(e).__name__}: {e}",
              flush=True)
    try:
        await session.async_close(force=True)
    except Exception as e:
        return f"could not close pane: {type(e).__name__}: {e}"
    KNOWN_AGENTS.pop(uuid, None)
    print(f"  [kill] closed pane {uuid}", flush=True)
    return None


def reap_clock(r):
    """When a pane was last interacted with: the last prompt or the last finish
    (work_since), else the record's own mtime. Session start is deliberately
    NOT a candidate — an old chat that is still being used is not stale."""
    return r.get("work_since") or r.get("mtime") or 0


@writes("reap")
async def api_reap(request):
    """Kill every session untouched for `hours` (default REAP_AFTER) — the reaper.

    Old means "nobody has talked to it for a while", NOT "started long ago": the
    clock is the last prompt sent or the last time it finished answering
    (work_since, the hook clock the yard timer runs on), so a chat opened this
    morning that is still being driven survives and one left idle since lunch
    goes. Ages come off the same rows the yard draws, so what gets killed is what
    the button counted. Oldest first, and one pane's failure never stops the sweep.
    """
    d = request.get("_body") or {}
    try:
        hours = float(d.get("hours") or REAP_AFTER / 3600)
    except (TypeError, ValueError):
        hours = REAP_AFTER / 3600
    cutoff = time.time() - max(0.0, hours) * 3600
    rows, _ = await build_fleet()
    sessions = await all_sessions()
    killed, failed = [], []
    for r in sorted(rows, key=reap_clock):
        last = reap_clock(r)
        if not last or last > cutoff:
            continue
        s = sessions.get(r["uuid"])
        if not s:
            continue                          # cached row, pane already gone
        err = await kill_pane(r["uuid"], s)
        rec = {"uuid": r["uuid"], "name": r.get("name") or "?"}
        if err:
            rec["error"] = err
            failed.append(rec)
        else:
            killed.append(rec)
    print(f"  [reap] over {hours}h: killed {len(killed)}, failed {len(failed)}",
          flush=True)
    return web.json_response({"ok": True, "hours": hours,
                              "killed": killed, "failed": failed})


EFFORTS = ("low", "medium", "high", "xhigh", "ultracode")
# Measured cycle (probe_mode.py): auto → manual → accept edits → plan → auto.
# "bypass" is separate — sessions launched with --dangerously-skip-permissions
# sit in it and it is not part of the Shift-Tab rotation.
MODES = ("manual", "auto", "accept", "plan", "bypass")
MODE_LABEL = {"manual": "manual", "auto": "auto",
              "accept": "accept edits", "plan": "plan", "bypass": "bypass"}


def detect_mode(text):
    """Read the current permission mode off Claude's status line."""
    for l in text.splitlines():
        low = l.lower()
        if "bypass permissions" in low: return "bypass"
        if "plan mode on" in low:       return "plan"
        if "accept edits on" in low:    return "accept"
        if "auto mode on" in low:       return "auto"
        if "manual mode on" in low:     return "manual"
    return None


GROK_MODES = ("ask", "plan", "auto", "always-approve")
GROK_MODE_SLASH = {
    "always-approve": "/always-approve",
    "auto": "/auto",
    "plan": "/plan",
}
GROK_CONFIG = os.path.expanduser("~/.grok/config.toml")
GROK_MODELS_CACHE = os.path.expanduser("~/.grok/models_cache.json")
_GROK_MODELS = {"at": 0, "models": []}
_GROK_MODEL_LOCK = asyncio.Lock()
_GROK_EFFORTS = ("low", "medium", "high", "xhigh")


@writes("mode")
async def api_mode(request):
    """Shift-Tab until the requested mode is showing.

    The cycle order isn't assumed — we re-read the status line after each press
    and stop when it matches, so this stays correct if Claude reorders modes.
    """
    body = await request.json()
    uuid, want = body.get("uuid", "").upper(), body.get("mode")
    if want not in MODES and want not in GROK_MODES:
        return web.json_response({"error": f"bad mode {want!r}"}, status=400)
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    if not is_agent_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no agent UI — refusing"},
            status=403)
    if provider_of(uuid) == "grok":
        return await _grok_mode(s, want)
    bad = claude_only(uuid, "mode")
    if bad:
        return bad

    if want == "bypass":
        return web.json_response(
            {"error": "bypass is not reachable via Shift-Tab; "
                      "it is set by launching with --dangerously-skip-permissions"},
            status=400)
    # From bypass we still TRY to reach plan/auto/accept/manual — those are wholly
    # separate modes and switching into a more restrictive one is safe. If Shift-Tab
    # genuinely can't leave bypass, the loop below reports "could not reach" honestly
    # rather than us refusing up front.
    seen = []
    for _ in range(len(MODES) + 1):
        cur = detect_mode(await pane_text(s))
        seen.append(cur)
        if cur == want:
            return web.json_response({"ok": True, "mode": cur, "path": seen})
        await s.async_send_text("\x1b[Z")
        await asyncio.sleep(0.7)
    final = detect_mode(await pane_text(s))
    if final == want:
        return web.json_response({"ok": True, "mode": final, "path": seen})
    # Never leave it spinning — report honestly rather than silently mis-set.
    return web.json_response(
        {"error": f"could not reach {want!r}; ended on {final!r}",
         "mode": final, "path": seen}, status=409)


@writes("effort")
async def api_effort(request):
    """Fire /effort <level>.

    Typing "/" opens Claude's autocomplete, which swallows the first Enter —
    so the sequence is text, Enter (accept completion), Enter (submit).
    Verified in probe_effort.py against all three levels.
    """
    body = await request.json()
    uuid, level = body.get("uuid", "").upper(), body.get("level")
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    if not is_agent_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no agent UI — refusing"},
            status=403)
    if provider_of(uuid) == "grok":
        if not level:
            return await _grok_open(s, "/effort", uuid)
        if not isinstance(level, str) or level not in _GROK_EFFORTS:
            return web.json_response({"error": f"bad level {level!r}"}, status=400)
        catalog = {m["id"]: m for m in await _grok_models()}
        status = detect_grok_status(await pane_text(s))
        current = _grok_match_model(catalog, status.get("model"))
        if current and current["efforts"] and level not in current["efforts"]:
            return web.json_response({"error": "effort is unsupported by this model"}, status=400)
        return await _grok_slash(s, f"/effort {level}", {"ok": True, "level": level})
    bad = claude_only(uuid, "effort")
    if provider_of(uuid) == "codex":
        model = body.get("model")
        if not level:
            if model is not None and not isinstance(model, str):
                return web.json_response({"error": "unsupported Codex model"}, status=400)
            return await _codex_change(s, model=model or None, ask=True, uuid=uuid)
        if not isinstance(level, str):
            return web.json_response({"error": "level is required"}, status=400)
        return await _codex_change(s, model=model if isinstance(model, str) else None,
                                   level=level, uuid=uuid)
    if bad:
        return bad
    if level not in EFFORTS:
        return web.json_response({"error": f"bad level {level!r}"}, status=400)
    await send_slash(s, f"/effort {level}")
    return web.json_response({"ok": True, "level": level})


MODELS = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku", "fable": "fable"}

# Allowlisted slash commands. Deliberately excludes anything that ends the
# session or is hard to undo from a phone (/exit, /logout, /doctor).
COMMANDS = {
    "clear":     ("/clear",     "wipe context"),
    "compact":   ("/compact",   "summarise + shrink"),
    "cost":      ("/cost",      "show spend"),
    "context":   ("/context",   "show context use"),
    "usage":     ("/usage",     "show limits"),
    "status":    ("/status",    "session status"),
    "mcp":       ("/mcp",       "list MCP servers"),
    "todos":     ("/todos",     "show todo list"),
    "help":      ("/help",      "list commands"),
    "release":   ("/release-notes", "what's new"),
}
CODEX_COMMANDS = {
    "clear":       ("/clear", "wipe context"),
    "model":       ("/model", "choose model and reasoning effort"),
    "permissions": ("/permissions", "choose permissions"),
}
PROVIDER_COMMANDS = {"claude": COMMANDS, "codex": CODEX_COMMANDS,
                     "grok": {"clear": ("/clear", "wipe context")}}


async def send_slash(s, text):
    """Type a slash command and submit it.

    The first Enter is eaten by Claude's autocomplete popup, so two are needed —
    see probe_slash.py, where single-Enter silently did nothing.
    """
    await s.async_send_text(text)
    await asyncio.sleep(0.9)
    await s.async_send_text("\r")
    await asyncio.sleep(0.6)
    await s.async_send_text("\r")


@writes("cmd")
async def api_cmd(request):
    body = await request.json()
    uuid, name = body.get("uuid", "").upper(), body.get("cmd")
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    provider = pane_provider(uuid, job, await pane_text(s))
    if not provider:
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no agent UI — refusing"},
            status=403)
    commands = PROVIDER_COMMANDS.get(provider, {})
    if name not in commands:
        return web.json_response({"error": f"command {name!r} not allowed"}, status=400)
    command = commands[name][0]
    if provider == "codex":
        # Codex opens the picker with one Enter; a second selects its first row.
        await s.async_send_text(command)
        await asyncio.sleep(0.9)
        await s.async_send_text("\r")
    else:
        await send_slash(s, command)
    return web.json_response({"ok": True, "cmd": command})


@guard
async def api_commands(request):
    provider = request.query.get("provider", "claude")
    if provider not in PROVIDER_COMMANDS:
        return web.json_response({"error": f"bad provider {provider!r}"}, status=400)
    return web.json_response(
        {"commands": [{"id": k, "cmd": v[0], "desc": v[1]}
                      for k, v in PROVIDER_COMMANDS[provider].items()]})


_CODEX_MODELS = {"at": 0, "models": []}
_CODEX_MODEL_LOCK = asyncio.Lock()
_CODEX_EFFORT_LABELS = {"low": "Low", "medium": "Medium", "high": "High",
                        "xhigh": "Extra high", "max": "Max", "ultra": "Ultra"}


async def _codex_models():
    async with _CODEX_MODEL_LOCK:
        if _CODEX_MODELS["models"] and time.monotonic() - _CODEX_MODELS["at"] < 60:
            return _CODEX_MODELS["models"]
        binary = agent_bin("codex")
        # launchd omits package-manager paths. The npm Codex launcher uses
        # /usr/bin/env node even when Codex itself was resolved absolutely.
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join(filter(None, [
            env.get("PATH", os.defpath), os.path.dirname(binary),
            "/opt/homebrew/bin", "/usr/local/bin"]))
        proc = await asyncio.create_subprocess_exec(
            binary, "debug", "models", env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError("Codex model discovery timed out")
        if proc.returncode:
            raise RuntimeError("Codex model discovery failed")
        data = json.loads(stdout)
        models = [{"id": m["slug"], "label": m["display_name"],
                   "efforts": [r["effort"] for r in m["supported_reasoning_levels"]],
                   "default_effort": m["default_reasoning_level"]}
                  for m in data["models"] if m.get("visibility") == "list"]
        if not models:
            raise RuntimeError("Codex returned no selectable models")
        _CODEX_MODELS.update(at=time.monotonic(), models=models)
        return models


@guard
async def api_codex_models(request):
    try:
        return web.json_response({"models": await _codex_models()})
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return web.json_response({"error": "Codex model discovery unavailable"}, status=503)


async def _grok_models():
    async with _GROK_MODEL_LOCK:
        if _GROK_MODELS["models"] and time.monotonic() - _GROK_MODELS["at"] < 60:
            return _GROK_MODELS["models"]
        models = []
        try:
            data = json.load(open(GROK_MODELS_CACHE))
        except (OSError, ValueError, TypeError):
            data = {}
        for key, entry in (data.get("models") or {}).items():
            info = (entry or {}).get("info") or {}
            if info.get("hidden"):
                continue
            mid = info.get("id") or key
            efforts = [e.get("id") for e in (info.get("reasoning_efforts") or []) if e.get("id")]
            default = next((e.get("id") for e in (info.get("reasoning_efforts") or [])
                            if e.get("default")), None)
            models.append({
                "id": mid,
                "label": info.get("name") or mid,
                "efforts": efforts or list(_GROK_EFFORTS),
                "default_effort": default or info.get("reasoning_effort") or "high",
            })
        if not models:
            raise RuntimeError("Grok model discovery unavailable")
        _GROK_MODELS.update(at=time.monotonic(), models=models)
        return models


@guard
async def api_grok_models(request):
    try:
        return web.json_response({"models": await _grok_models()})
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return web.json_response({"error": "Grok model discovery unavailable"}, status=503)


def _grok_match_model(catalog, name):
    if not name or not isinstance(name, str):
        return None
    key = name.strip().lower()
    if key in catalog:
        return catalog[key]
    for model in catalog.values():
        if (model["label"] or "").strip().lower() == key:
            return model
    compact = re.sub(r"[^a-z0-9.]+", "", key)
    for model in catalog.values():
        ids = (model["id"], model["label"])
        if any(re.sub(r"[^a-z0-9.]+", "", (x or "").lower()) == compact for x in ids):
            return model
    return None


def _read_grok_default():
    try:
        text = open(GROK_CONFIG).read()
    except OSError:
        return _MISSING
    section, in_models = None, False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_models = stripped.strip("[]").strip() == "models"
            continue
        if in_models:
            m = re.match(r'default\s*=\s*"(.*?)"', stripped)
            if m:
                return m.group(1)
    return _MISSING


def _restore_grok_default(prev):
    if prev is _MISSING:
        return
    try:
        text = open(GROK_CONFIG).read()
    except OSError:
        return
    lines, out, in_models, done = text.splitlines(True), [], False, False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_models = stripped.strip("[]").strip() == "models"
        elif in_models and not done:
            m = re.match(r'(\s*default\s*=\s*")(.*?)(".*)$', line.rstrip("\n"))
            if m and m.group(2) != prev:
                line = f'{m.group(1)}{prev}{m.group(3)}\n'
                done = True
        out.append(line)
    if not done:
        return
    tmp = GROK_CONFIG + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write("".join(out))
        os.replace(tmp, GROK_CONFIG)
    except OSError as e:
        print(f"  [model] could not restore Grok default: {type(e).__name__}: {e}",
              flush=True)


async def _keep_grok_default(prev):
    for _ in range(24):
        await asyncio.sleep(0.25)
        if _read_grok_default() != prev:
            break
    await asyncio.sleep(0.4)
    _restore_grok_default(prev)


async def _grok_focus(s):
    """Grok's slash menu only works with the prompt focused. Scrollback focus
    advertises `Space:prompt` on the shortcuts bar."""
    text = await pane_text(s)
    if re.search(r"Space:prompt", text, re.I):
        await s.async_send_text(" ")
        await asyncio.sleep(0.25)


async def send_grok_slash(s, text):
    """Type a Grok slash command and submit it with one Enter.

    Grok's autocomplete runs the highlighted command on the first Enter, so a
    second Enter (what Claude needs) would send an empty follow-up. A fully
    typed `/model grok-4.6` submits on that first Enter.
    """
    await _grok_focus(s)
    await s.async_send_text("\x15")
    await asyncio.sleep(0.15)
    await s.async_send_text(text)
    await asyncio.sleep(0.6)
    await s.async_send_text("\r")


async def _grok_wait(s, predicate):
    for _ in range(50):
        text = await pane_text(s)
        value = predicate(text)
        if value:
            return text, value
        await asyncio.sleep(0.15)
    raise RuntimeError("Grok did not reach the expected screen; try again")


async def _grok_slash(s, command, payload):
    if _grok_prompt(await pane_text(s)):
        return web.json_response({"error": "Grok has an open picker — cancel it first"},
                                 status=409)
    await send_grok_slash(s, command)
    return web.json_response(payload)


async def _grok_open(s, command, uuid):
    initial = await pane_text(s)
    if _grok_prompt(initial):
        return web.json_response({"error": "Grok has an open picker — cancel it first"},
                                 status=409)
    await _grok_focus(s)
    await s.async_send_text("\x15")
    await asyncio.sleep(0.15)
    await s.async_send_text(command)
    await asyncio.sleep(0.6)
    await s.async_send_text("\r")
    try:
        text, _ = await _grok_wait(s, _grok_prompt)
    except RuntimeError:
        await s.async_send_text("\x1b")
        return web.json_response(
            {"error": "Grok did not open the picker; try again"}, status=409)
    return web.json_response({"ok": True, "prompt": detect_prompt(text, "grok", uuid)})


async def _grok_mode(s, want):
    if want not in GROK_MODES:
        return web.json_response({"error": f"bad mode {want!r}"}, status=400)
    if _grok_prompt(await pane_text(s)):
        return web.json_response({"error": "Grok has an open picker — cancel it first"},
                                 status=409)
    seen = []
    for _ in range(len(GROK_MODES) + 2):
        cur = detect_grok_status(await pane_text(s)).get("mode") or "ask"
        seen.append(cur)
        if cur == want:
            return web.json_response({"ok": True, "mode": cur, "path": seen})
        await s.async_send_text("\x1b[Z")
        await asyncio.sleep(0.7)
    final = detect_grok_status(await pane_text(s)).get("mode") or "ask"
    if final == want:
        return web.json_response({"ok": True, "mode": final, "path": seen})
    slash = GROK_MODE_SLASH.get(want)
    if slash:
        await send_grok_slash(s, slash)
        await asyncio.sleep(0.5)
        final = detect_grok_status(await pane_text(s)).get("mode") or "ask"
        if final == want:
            return web.json_response({"ok": True, "mode": final, "path": seen + [final]})
        if want == "ask":
            # Toggles: turn off whichever elevated mode is showing.
            for cmd in ("/always-approve", "/auto"):
                await send_grok_slash(s, cmd)
                await asyncio.sleep(0.5)
                final = detect_grok_status(await pane_text(s)).get("mode") or "ask"
                if final == "ask":
                    return web.json_response({"ok": True, "mode": final, "path": seen + [final]})
    return web.json_response(
        {"error": f"could not reach {want!r}; ended on {final!r}",
         "mode": final, "path": seen}, status=409)


async def _grok_prompt_action(s, uuid, prompt, action, option_index=None):
    identity = prompt["id"]

    def same(p):
        return p is not None and p.get("id") == identity

    if action == "cancel":
        await s.async_send_text("\x1b")
        for _ in range(20):
            fresh = detect_prompt(await pane_text(s), "grok", uuid)
            if not same(fresh):
                return fresh
            await asyncio.sleep(0.15)
        raise RuntimeError("Grok prompt did not cancel")
    selected = next((o["index"] for o in prompt["options"] if o["selected"]), None)
    if selected is None:
        raise RuntimeError("Grok option caret is not visible")
    target = option_index
    if target is None:
        raise RuntimeError("option index is required")
    for _ in range(len(prompt["options"])):
        if selected == target:
            break
        step = 1 if target > selected else -1
        await s.async_send_text("\x1b[B" if step > 0 else "\x1b[A")
        selected += step
        for _ in range(20):
            fresh = detect_prompt(await pane_text(s), "grok", uuid)
            if same(fresh) and any(o["index"] == selected and o["selected"] for o in fresh["options"]):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Grok option caret did not move")
    await s.async_send_text("\r")
    for _ in range(20):
        fresh = detect_prompt(await pane_text(s), "grok", uuid)
        if not same(fresh):
            return fresh
        await asyncio.sleep(0.15)
    raise RuntimeError("Grok prompt did not accept the choice")


def _codex_title_matches(line, title):
    line = line.strip()
    if line == title:
        return True
    # "Select Model" must not match "Select Model and Effort".
    if title in ("Select Model", "Select Model and Effort", "Advanced Reasoning",
                 "Apply reasoning change"):
        return False
    return line.startswith(title + " ") or line.startswith(title + " for")


def _codex_picker(text, title):
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if _codex_title_matches(line, title)]
    if not starts:
        return None
    run = _last_numbered_run(lines[starts[-1] + 1:])
    return run


def _codex_row_slug(row):
    return re.split(r"\s{2,}", row[2], maxsplit=1)[0].split()[0]


def _codex_wanted_rows(rows, catalog, model):
    candidates = []
    for row in rows:
        slug = _codex_row_slug(row)
        if slug in catalog and (slug == model if model else "(current)" in row[2]):
            candidates.append((row, slug))
    return candidates


async def _codex_open_model_list(s, catalog, model):
    """Drive /model to a list that contains the requested (or current) model.

    Codex 0.154 opens 'Select Model' (auto modes + All models) first. Older
    builds go straight to 'Select Model and Effort'.
    """
    await s.async_send_text("/model")
    await asyncio.sleep(0.9)
    await s.async_send_text("\r")
    text, _ = await _codex_wait(
        s, lambda t: _codex_picker(t, "Select Model and Effort") or _codex_picker(t, "Select Model"))
    full = _codex_picker(text, "Select Model and Effort")
    if full:
        return text, full
    rows = _codex_picker(text, "Select Model")
    if _codex_wanted_rows(rows, catalog, model):
        return text, rows
    all_rows = [row for row in rows if re.match(r"All models\b", row[2].strip(), re.I)]
    if len(all_rows) != 1:
        return text, rows
    await s.async_send_text(all_rows[0][1])
    return await _codex_wait(s, lambda t: _codex_picker(t, "Select Model and Effort"))


def _codex_live_tail(text, n=8):
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-n:])


def _codex_busy(text):
    # Only the live footer — older "Working (esc to interrupt)" lines stay in
    # the visible pane after a run ends and must not block /model. The elapsed
    # `(12s • esc to interrupt)` shape is what Codex paints while a turn runs;
    # a question footer also says "esc to interrupt" and must not match.
    # A placeholder composer *below* a progress line is the idle signal; the
    # same line holding both (0.153 redraws them together) is still a live turn.
    lines = [line for line in (text or "").splitlines() if line.strip()][-5:]
    last_progress = last_placeholder = -1
    for i, line in enumerate(lines):
        if _CODEX_PROGRESS_RE.search(line):
            last_progress = i
        stripped = line.lstrip()
        if stripped.startswith("›") and not OPTION_RE.match(line):
            value = stripped[1:].lstrip()
            if any(value == p or value.startswith(p) for p in _CODEX_PLACEHOLDERS):
                last_placeholder = i
    return last_progress >= 0 and last_progress >= last_placeholder


def _codex_live_picker(text):
    titles = []
    for line in text.splitlines():
        t = line.strip()
        if _CODEX_PICKER_TITLES.match(t):
            titles.append(t)
    return any(_codex_picker(text, title) for title in titles)


def _codex_idle(text):
    if detect_prompt(text, "codex") or _codex_live_picker(text) or _codex_busy(text):
        return False
    # The empty Codex composer shows a native placeholder. A typed draft is
    # deliberately not accepted, even if a prior prompt is still in scrollback.
    visible = any(line.lstrip().startswith("›") and not OPTION_RE.match(line)
                  for line in text.splitlines())
    ghost, draft = _codex_input(text)
    return visible and (ghost or not draft)


async def _codex_dismiss_open_ui(s):
    for _ in range(6):
        text = await pane_text(s)
        if not detect_prompt(text, "codex") and not _codex_live_picker(text):
            return
        await s.async_send_text("\x1b")
        await asyncio.sleep(0.15)


async def _codex_wait(s, predicate):
    for _ in range(50):
        text = await pane_text(s)
        value = predicate(text)
        if value:
            return text, value
        await asyncio.sleep(0.15)
    raise RuntimeError("Codex did not reach the expected screen; try again")


async def _codex_change(s, model=None, level=None, ask=False, uuid=None):
    try:
        catalog = {m["id"]: m for m in await _codex_models()}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return web.json_response({"error": "Codex model discovery unavailable"}, status=503)
    if model is not None and model not in catalog:
        if ask:
            model = None
        else:
            return web.json_response({"error": "unsupported Codex model"}, status=400)
    if not ask:
        if level is not None and (not isinstance(level, str) or level not in _CODEX_EFFORT_LABELS
                                 or not any(level in m["efforts"] for m in catalog.values())):
            return web.json_response({"error": "unsupported Codex reasoning effort"}, status=400)
        if model is not None and level is not None and level not in catalog[model]["efforts"]:
            return web.json_response({"error": "effort is unsupported by this model"}, status=400)
    initial = await pane_text(s)
    if _codex_busy(initial):
        return web.json_response({"error": "Codex is working — wait until it finishes"}, status=409)
    if detect_prompt(initial, "codex") or _codex_live_picker(initial):
        return web.json_response({"error": "Codex has an open picker — cancel it first"}, status=409)
    if not _codex_idle(initial):
        ghost, draft = _codex_input(initial)
        if not draft or ghost:
            return web.json_response(
                {"error": "Codex must be idle with an empty composer and no open picker"}, status=409)
        await s.async_send_text("\x15")
        try:
            initial, _ = await _codex_wait(s, _codex_idle)
        except RuntimeError:
            await s.async_send_text("\x03")
            try:
                initial, _ = await _codex_wait(s, _codex_idle)
            except RuntimeError:
                return web.json_response(
                    {"error": "Codex must be idle with an empty composer and no open picker"}, status=409)
    owned = False
    left_open = False
    titles = {"Select Model", "Select Model and Effort", "Select Reasoning Level"}
    try:
        owned = True
        _, rows = await _codex_open_model_list(s, catalog, model)
        candidates = _codex_wanted_rows(rows, catalog, model)
        if len(candidates) != 1:
            raise RuntimeError("requested Codex model is not uniquely visible in the picker")
        row, model = candidates[0]
        titles.add(f"Select Reasoning Level for {model}")
        await s.async_send_text(row[1])
        text, rows = await _codex_wait(s, lambda t: _codex_picker(t, "Select Reasoning Level"))
        if ask:
            left_open = True
            prompt = detect_prompt(text, "codex", uuid)
            return web.json_response({"ok": True, "model": model, "prompt": prompt})
        level = level or catalog[model]["default_effort"]
        if level not in catalog[model]["efforts"] or level not in _CODEX_EFFORT_LABELS:
            raise RuntimeError("effort is unsupported by the current Codex model")
        if level in ("max", "ultra"):
            more = [r for r in rows if r[2].startswith("More reasoning")]
            if len(more) != 1:
                raise RuntimeError("advanced reasoning is not visible in the Codex picker")
            titles.add("Advanced Reasoning")
            await s.async_send_text(more[0][1])
            _, rows = await _codex_wait(s, lambda t: _codex_picker(t, "Advanced Reasoning"))
        label = _CODEX_EFFORT_LABELS[level]
        matches = [r for r in rows if re.fullmatch(
            re.escape(label) + r"(?:\s*\([^)]*\))*", re.split(r"\s{2,}", r[2])[0])]
        if len(matches) != 1:
            raise RuntimeError("requested reasoning level is not uniquely visible in the Codex picker")
        before = await pane_text(s)
        completion = re.compile(r"Model changed to\s+" + re.escape(model) + r"\s+" + re.escape(level) + r"\b")
        count = max(len(completion.findall(before)), len(completion.findall(initial)))
        await s.async_send_text(matches[0][1])
        await _codex_wait(s, lambda t: not detect_prompt(t, "codex") and len(completion.findall(t)) > count)
        await _codex_dismiss_open_ui(s)
        return web.json_response({"ok": True, "model": model, "level": level, "prompt": None})
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    finally:
        if owned and not left_open:
            await _codex_dismiss_open_ui(s)


SETTINGS_JSON = os.path.expanduser("~/.claude/settings.json")
_MISSING = object()
_settings_lock = asyncio.Lock()


def _read_default_model():
    """Current `model` in ~/.claude/settings.json, or _MISSING if unset."""
    try:
        with open(SETTINGS_JSON) as f:
            return json.load(f).get("model", _MISSING)
    except Exception:
        return _MISSING


def _restore_default_model(prev):
    """Put the global default back to `prev` (_MISSING = remove the key).

    Re-reads the file first so we only rewrite that one key and keep whatever
    else Claude has written since; atomic replace, as in trust_dir.
    """
    try:
        with open(SETTINGS_JSON) as f:
            cfg = json.load(f)
    except Exception:
        return
    if cfg.get("model", _MISSING) == prev:
        return                              # nothing moved; leave the file alone
    if prev is _MISSING:
        cfg.pop("model", None)
    else:
        cfg["model"] = prev
    tmp = SETTINGS_JSON + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, SETTINGS_JSON)
    except Exception as e:
        print(f"  [model] could not restore global default: "
              f"{type(e).__name__}: {e}", flush=True)


async def _keep_model_session_local(prev):
    """Undo /model's write to the global default, leaving the pane switched.

    /model changes the running pane AND rewrites `model` in settings.json. We
    snapshot that key before firing, wait for Claude to land its write, then put
    the old value back — so the switch only sticks to the pane that asked.
    """
    for _ in range(24):                      # ~6s, polled every 250ms
        await asyncio.sleep(0.25)
        if _read_default_model() != prev:
            break
    await asyncio.sleep(0.4)                 # let the write settle
    _restore_default_model(prev)


@writes("model")
async def api_model(request):
    """Fire /model <name> for one pane only.

    /model also rewrites ~/.claude/settings.json, which would repoint every
    FUTURE session. We snapshot the old default and restore it once Claude has
    written, so the change stays local to this pane.
    """
    body = await request.json()
    uuid, name = body.get("uuid", "").upper(), body.get("model")
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    if not is_agent_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no agent UI — refusing"},
            status=403)
    if provider_of(uuid) == "grok":
        if not name:
            return await _grok_open(s, "/model", uuid)
        if not isinstance(name, str):
            return web.json_response({"error": "model is required"}, status=400)
        catalog = {m["id"]: m for m in await _grok_models()}
        model = _grok_match_model(catalog, name)
        if not model:
            return web.json_response({"error": "unsupported Grok model"}, status=400)
        prev = _read_grok_default()
        result = await _grok_slash(s, f"/model {model['id']}",
                                   {"ok": True, "model": model["id"]})
        asyncio.ensure_future(_keep_grok_default(prev))
        return result
    bad = claude_only(uuid, "model")
    if provider_of(uuid) == "codex":
        if not isinstance(name, str) or not name:
            return web.json_response({"error": "model is required"}, status=400)
        return await _codex_change(s, model=name, level=body.get("effort"))
    if bad:
        return bad
    if name not in MODELS:
        return web.json_response({"error": f"bad model {name!r}"}, status=400)
    async with _settings_lock:
        prev = _read_default_model()
        await send_slash(s, f"/model {MODELS[name]}")
        await _keep_model_session_local(prev)
    return web.json_response({"ok": True, "model": name})


# ── usage / CC Dash data ───────────────────────────────────────────────────
# Claude reuses ~/.claude/cc_history.py — the same module cc-dashboard.py reads,
# so the numbers here and in the TUI cannot drift apart. Codex and Grok have no
# equivalent module; their journals (~/.codex/sessions rollouts, ~/.grok/sessions
# usage.json) are scanned the same way the fleet already reads them for ops.
sys.path.insert(0, os.path.expanduser("~/.claude"))
_usage_cache = {}                 # provider -> {"at": epoch, "data": dict}
USAGE_TTL = 120
GROK_COST_TICKS = 1e10            # grok usage.json: divide costUsdTicks by this
_CODEX_SID_RE = re.compile(
    r"rollout-.*-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$")
_native_scan_cache = {}           # path -> {"k":[mtime,size], "pack": dict}


def _parse_ts(ts):
    """ISO timestamp or unix seconds → epoch, else None."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _local_day(ts):
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _produced_tokens(inp, cached, cache_write, out):
    """New work: uncached input + output + cache writes. Cache reads are replay."""
    return max(0, (inp or 0) - (cached or 0)) + (out or 0) + (cache_write or 0)


def _short_native_model(m):
    if not m:
        return ""
    return re.sub(r"-build$", "", str(m))


def _paths_key(*paths):
    mt = sz = 0
    ok = False
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        ok = True
        if st.st_mtime > mt:
            mt = st.st_mtime
        sz += st.st_size
    return [mt, sz] if ok else None


def _cached_pack(path, key, builder):
    c = _native_scan_cache.get(path)
    if c and c["k"] == key:
        return c["pack"]
    pack = builder()
    _native_scan_cache[path] = {"k": key, "pack": pack}
    return pack


def _empty_days():
    from datetime import date as _date
    from datetime import timedelta as _td
    _t = _date.today()
    return [(_t - _td(days=i)).isoformat() for i in range(29, -1, -1)]


def _usage_from_packs(packs, limits=None):
    """today / daily-30 / all-time from native session packs (Codex or Grok)."""
    today = time.strftime("%Y-%m-%d")
    days = _empty_days()
    acc = {d: {"cost": 0.0, "tokens": 0} for d in days}
    tot_tokens = tot_cost = sessions = 0
    active = set()
    limit_maps = []
    for pack in packs:
        if not pack:
            continue
        hist = pack.get("hist")
        if hist:
            sessions += 1
            tot_tokens += hist.get("tokens") or 0
            tot_cost += hist.get("cost") or 0
        else:
            tot_tokens += pack.get("tokens") or 0
            tot_cost += pack.get("cost") or 0
        if pack.get("limits"):
            limit_maps.append(pack["limits"])
        for d, v in (pack.get("days") or {}).items():
            if isinstance(v, dict):
                tok, cost = v.get("tokens") or 0, v.get("cost") or 0
            else:
                tok, cost = v or 0, 0
            if tok or cost:
                active.add(d)
            if d in acc:
                acc[d]["tokens"] += tok
                acc[d]["cost"] += cost
    daily = [{"d": d, "cost": round(acc[d]["cost"], 2),
              "tokens": acc[d]["tokens"]} for d in days]
    if limits is None:
        limits = fleet_limits({i: {"limits": m} for i, m in enumerate(limit_maps)})
    return {
        "today": {"tokens": acc.get(today, {}).get("tokens", 0),
                  "cost": round(acc.get(today, {}).get("cost", 0.0), 2)},
        "daily": daily,
        "all_time": {
            "tokens": tot_tokens,
            "cost": round(tot_cost, 2),
            "sessions": sessions,
            "active_days": len(active),
        },
        "limits": limits,
    }


def _claude_account_limits():
    """Claude 5h/7d from FLEET_DIR dumps, including panes past the 90s STALE cutoff.

    Billing windows expire in fleet_limits; pane liveness is not the same clock.
    """
    files = {}
    for path in glob.glob(os.path.join(FLEET_DIR, "*.json")):
        try:
            d = json.load(open(path))
        except Exception:
            continue
        if d.get("provider") not in (None, "claude"):
            continue
        rl = d.get("rate_limits")
        if not isinstance(rl, dict):
            continue
        files[path] = {"limits": rl}
    return fleet_limits(files)


def _compute_usage_claude():
    # Only today. The dashboard is a "what is happening right now" screen — the
    # 30-day chart, per-project spend and all-time totals live in the TUI.
    import cc_history as HIST
    agg = HIST.build()          # build() already returns the aggregate
    tokens = HIST.series(agg, "tokens")
    cost = HIST.series(agg, "cost")
    today = time.strftime("%Y-%m-%d")
    tot = agg.get("tot") or {}
    days = _empty_days()
    daily = [{"d": dd, "cost": round(cost.get(dd, 0.0), 2),
              "tokens": tokens.get(dd, 0)} for dd in days]
    return {
        "today": {"tokens": tokens.get(today, 0),
                  "cost": round(cost.get(today, 0.0), 2)},
        "daily": daily,
        # all-time roll-up: tokens counts in+out+cache-create (cache reads are the
        # replayed context, not new work), matching cc-dashboard's totals line.
        "all_time": {
            "tokens": (tot.get("in", 0) + tot.get("out", 0) + tot.get("cc", 0)),
            "cost": round(agg.get("cost", 0.0) or 0.0, 2),
            "sessions": agg.get("sessions", 0),
            "active_days": len(HIST.active_days(agg))
                           if hasattr(HIST, "active_days") else 0,
        },
        "limits": _claude_account_limits(),
    }


def _codex_rate_limits(rl):
    """Codex token_count.rate_limits → five_hour/seven_day/monthly by window."""
    if not isinstance(rl, dict):
        return {}
    now = time.time()
    out = {}
    windows = {300: "five_hour", 10080: "seven_day", 43200: "monthly"}
    for src, fallback in (("primary", "five_hour"), ("secondary", "seven_day")):
        w = rl.get(src)
        if not isinstance(w, dict):
            continue
        pct = w.get("used_percent")
        if pct is None:
            pct = w.get("used_percentage")
        if pct is None:
            continue
        resets = w.get("resets_at") or 0
        if resets and resets <= now:
            continue
        wm = w.get("window_minutes")
        dst = fallback if wm is None else windows.get(wm)
        if not dst:
            continue
        out[dst] = {"used_percentage": pct, "resets_at": resets}
    return out


def _scan_codex_file(path):
    """One Codex rollout → history card + per-day produced tokens + latest limits.

    token_usage_record.usage is per-response (input includes cached reads).
    Daily/all-time tokens count new work only, matching Claude's in+out+cc.
    """
    days = {}
    itok = otok = cached = cwrite = 0
    cwd = model = sid = None
    first = last = None
    title = first_title = ""
    prompts = 0
    blurb = []; blurb_len = 0
    limits = {}
    try:
        fh = open(path, errors="ignore")
    except OSError:
        return None
    with fh:
        for line in fh:
            if not line or not any(s in line for s in (
                    "session_meta", "token_usage_record", "token_count",
                    '"role":"user"', '"role": "user"', "turn_context")):
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            ts = _parse_ts(o.get("timestamp"))
            if ts:
                if first is None or ts < first:
                    first = ts
                if last is None or ts > last:
                    last = ts
            typ = o.get("type")
            p = o.get("payload") if isinstance(o.get("payload"), dict) else {}
            if typ == "session_meta":
                sid = p.get("session_id") or p.get("id") or sid
                cwd = p.get("cwd") or cwd
                continue
            if typ == "turn_context":
                model = p.get("model") or model
                cwd = p.get("cwd") or cwd
                continue
            if typ == "token_usage_record" or (typ == "event_msg" and
                                                p.get("type") == "token_count"):
                if typ == "token_usage_record":
                    u = p.get("usage") if isinstance(p.get("usage"), dict) else {}
                else:
                    info = p.get("info") if isinstance(p.get("info"), dict) else {}
                    u = info.get("last_token_usage") if isinstance(
                        info.get("last_token_usage"), dict) else {}
                i = u.get("input_tokens") or 0
                c = u.get("cached_input_tokens") or 0
                w = u.get("cache_write_input_tokens") or 0
                ot = u.get("output_tokens") or 0
                itok += i; cached += c; cwrite += w; otok += ot
                produced = _produced_tokens(i, c, w, ot)
                if ts:
                    slot = days.setdefault(_local_day(ts),
                                           {"tokens": 0, "cost": 0.0})
                    slot["tokens"] += produced
                    slot["cost"] += _codex_cost(model, i, c, w, ot)
                if typ == "event_msg":
                    limits = _codex_rate_limits(p.get("rate_limits"))
                continue
            if typ == "event_msg" and p.get("type") == "token_count":
                limits = _codex_rate_limits(p.get("rate_limits"))
                continue
            if not (typ == "response_item" and p.get("type") == "message"
                    and p.get("role") == "user"):
                continue
            txt = " ".join(x.get("text", "") for x in (p.get("content") or [])
                           if isinstance(x, dict) and x.get("type") == "input_text")
            clean = human_prompt(txt)
            if not clean:
                continue
            prompts += 1
            clean = clean.replace("\n", " ")
            title = clean[:120]
            if not first_title:
                first_title = title
            if blurb_len < 1600:
                take = clean[:300]
                blurb.append(take); blurb_len += len(take)
    if last is None:
        return None
    if not sid:
        m = _CODEX_SID_RE.search(os.path.basename(path))
        sid = m.group(1) if m else os.path.basename(path)[:-6]
    tokens = _produced_tokens(itok, cached, cwrite, otok)
    cost = _codex_cost(model, itok, cached, cwrite, otok)
    ops = codex_ops(path)
    return {
        "hist": {
            "session_id": sid,
            "title": title or "(no prompt)",
            "cwd": cwd or "",
            "project": os.path.basename((cwd or "").rstrip("/")) if cwd else "",
            "model": _short_native_model(model),
            "prompts": prompts,
            "tokens": tokens,
            "cost": round(cost, 2),
            "files": len(ops["files"]),
            "lines_add": ops["add"],
            "lines_del": ops["del"],
            "last": last, "first": first,
            "search": " ".join(blurb)[:1600],
            "provider": "codex",
        },
        "days": days,
        "limits": limits,
        "tokens": tokens,
        "cost": cost,
    }


def _codex_pack(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return _cached_pack(path, [st.st_mtime, st.st_size],
                        lambda: _scan_codex_file(path))


def _scan_grok_session(sdir):
    """One Grok session dir → history card + per-day tokens/cost from usage.json.

    costUsdTicks is 10^10 ticks per USD. Turns carry endedAt, so the 30-day
    chart is turn-bucketed; all-time totals prefer the session roll-up.
    """
    summary_path = os.path.join(sdir, "summary.json")
    usage_path = os.path.join(sdir, "usage.json")
    chat_path = os.path.join(sdir, "chat_history.jsonl")
    summary = {}
    try:
        if os.path.exists(summary_path):
            summary = json.loads(pathlib.Path(summary_path).read_text())
    except Exception:
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    info = summary.get("info") if isinstance(summary.get("info"), dict) else {}
    sid = info.get("id") or os.path.basename(sdir)
    cwd = info.get("cwd") or ""
    model = summary.get("current_model_id") or ""
    title = (summary.get("generated_title") or summary.get("session_summary")
             or "").strip()
    first = _parse_ts(summary.get("created_at"))
    last = _parse_ts(summary.get("last_active_at") or summary.get("updated_at"))

    usage = {}
    try:
        if os.path.exists(usage_path):
            usage = json.load(open(usage_path))
    except Exception:
        usage = {}
    if not isinstance(usage, dict):
        usage = {}
    sess = usage.get("session") if isinstance(usage.get("session"), dict) else {}
    cost = (sess.get("costUsdTicks") or 0) / GROK_COST_TICKS
    tokens = _produced_tokens(sess.get("inputTokens") or 0,
                              sess.get("cachedReadTokens") or 0,
                              sess.get("cacheCreationTokens") or 0,
                              sess.get("outputTokens") or 0)
    days = {}
    for t in usage.get("turns") or []:
        if not isinstance(t, dict):
            continue
        ts = _parse_ts(t.get("endedAt"))
        if not ts:
            continue
        if first is None or ts < first:
            first = ts
        if last is None or ts > last:
            last = ts
        slot = days.setdefault(_local_day(ts), {"tokens": 0, "cost": 0.0})
        slot["tokens"] += _produced_tokens(
            t.get("inputTokens") or 0, t.get("cachedReadTokens") or 0,
            t.get("cacheCreationTokens") or 0, t.get("outputTokens") or 0)
        slot["cost"] += (t.get("costUsdTicks") or 0) / GROK_COST_TICKS
    if not days and (tokens or cost) and last:
        days[_local_day(last)] = {"tokens": tokens, "cost": cost}

    prompts = 0
    blurb = []; blurb_len = 0
    last_prompt = ""
    try:
        with open(chat_path, errors="ignore") as fh:
            for line in fh:
                if '"type":"user"' not in line and '"type": "user"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "user":
                    continue
                txt = " ".join(x.get("text", "") for x in (o.get("content") or [])
                               if isinstance(x, dict) and x.get("type") == "text")
                clean = human_prompt(txt)
                if not clean:
                    continue
                prompts += 1
                clean = clean.replace("\n", " ")
                last_prompt = clean[:120]
                if blurb_len < 1600:
                    take = clean[:300]
                    blurb.append(take); blurb_len += len(take)
    except OSError:
        pass
    if not title:
        title = last_prompt
    if last is None:
        return None
    ops = grok_ops(chat_path)
    search = " ".join([
        title,
        summary.get("session_summary") or "",
        summary.get("last_turn_summary") or "",
        *blurb,
    ])[:1600]
    return {
        "hist": {
            "session_id": sid,
            "title": (title or "(no prompt)")[:120],
            "cwd": cwd,
            "project": os.path.basename(cwd.rstrip("/")) if cwd else "",
            "model": _short_native_model(model),
            "prompts": prompts,
            "tokens": tokens,
            "cost": round(cost, 2),
            "files": len(ops["files"]),
            "lines_add": ops["add"],
            "lines_del": ops["del"],
            "last": last, "first": first,
            "search": search,
            "provider": "grok",
        },
        "days": days,
        "limits": {},
        "tokens": tokens,
        "cost": cost,
    }


def _grok_pack(sdir):
    key = _paths_key(os.path.join(sdir, "summary.json"),
                     os.path.join(sdir, "usage.json"),
                     os.path.join(sdir, "chat_history.jsonl"))
    if key is None:
        return None
    return _cached_pack(sdir, key, lambda: _scan_grok_session(sdir))


def _iter_codex_packs():
    files = glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", "*.jsonl"))
    live = set(files)
    packs = []
    for p in files:
        pack = _codex_pack(p)
        if pack:
            packs.append(pack)
    return packs, live


def _iter_grok_packs():
    dirs = [d for d in glob.glob(os.path.join(GROK_SESSIONS, "*", "*"))
            if os.path.isdir(d)]
    live = set(dirs)
    packs = []
    for d in dirs:
        pack = _grok_pack(d)
        if pack:
            packs.append(pack)
    return packs, live


def _compute_usage_codex():
    packs, live = _iter_codex_packs()
    for gone in [p for p in _native_scan_cache
                 if p.startswith(CODEX_SESSIONS) and p not in live]:
        _native_scan_cache.pop(gone, None)
    return _usage_from_packs(packs)


def _grok_billing_limits():
    """Account weekly (or monthly) credit fill, from Grok's own billing fetch.

    Grok logs `billing: fetched credits config` on the same payload /usage shows:
    creditUsagePercent + currentPeriod.{type,start,end}. SuperGrok is a shared
    weekly pool — there is no 5-hour window. Tail the log rather than calling
    grok.com so we never handle the auth token.
    """
    try:
        with open(GROK_LOG, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 1_000_000))
            chunk = fh.read().decode("utf-8", "ignore")
    except OSError:
        return {}
    last = None
    for line in chunk.splitlines():
        if "billing: fetched credits config" not in line:
            continue
        try:
            last = json.loads(line)
        except Exception:
            continue
    if not last:
        return {}
    cfg = (last.get("ctx") or {}).get("config") or {}
    pct = cfg.get("creditUsagePercent")
    if pct is None:
        return {}
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    kind = (period.get("type") or "USAGE_PERIOD_TYPE_WEEKLY").replace(
        "USAGE_PERIOD_TYPE_", "").lower()
    resets = _parse_ts(period.get("end") or cfg.get("billingPeriodEnd"))
    window = {"used_percentage": pct, "resets_at": resets}
    out = {kind: window}
    if kind == "weekly":
        out["seven_day"] = window
    elif kind == "monthly":
        out["monthly"] = window
    return out


def _grok_session_limits():
    """Hottest recently-active Grok session's context-window fill.

    signals.json.contextWindowUsage is the same percentage Grok's /usage
    'Context usage' tab shows. Stale files (older than STALE_GENERIC) are
    ignored so a finished chat does not keep the bar pegged.
    """
    best = None
    now = time.time()
    for d in glob.glob(os.path.join(GROK_SESSIONS, "*", "*")):
        sigp = os.path.join(d, "signals.json")
        try:
            if now - os.path.getmtime(sigp) > STALE_GENERIC:
                continue
            sig = json.load(open(sigp))
        except (OSError, ValueError, TypeError):
            continue
        pct = sig.get("contextWindowUsage")
        if pct is None:
            continue
        if best is None or pct > best:
            best = pct
    return {"session": {"used_percentage": best}} if best is not None else {}


def _compute_usage_grok():
    packs, live = _iter_grok_packs()
    for gone in [p for p in _native_scan_cache
                 if p.startswith(GROK_SESSIONS) and p not in live]:
        _native_scan_cache.pop(gone, None)
    limits = {}
    limits.update(_grok_session_limits())
    limits.update(_grok_billing_limits())
    return _usage_from_packs(packs, limits=limits)


def _compute_usage(provider=DEFAULT_PROVIDER):
    if provider == "codex":
        return _compute_usage_codex()
    if provider == "grok":
        return _compute_usage_grok()
    return _compute_usage_claude()


@guard
async def api_usage(request):
    provider = (request.query.get("provider") or DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDERS:
        return web.json_response(
            {"error": f"unknown provider {provider!r}"}, status=400)
    now = time.time()
    cache = _usage_cache.get(provider) or {}
    if cache.get("data") and now - cache.get("at", 0) < USAGE_TTL:
        data = cache["data"]
        computed_at = cache["at"]
    else:
        try:
            data = await asyncio.to_thread(_compute_usage, provider)
            _usage_cache[provider] = {"at": now, "data": data}
            computed_at = now
        except Exception as e:
            print(f"  [usage] failed ({provider}): {type(e).__name__}: {e}",
                  flush=True)
            return web.json_response({"error": f"{type(e).__name__}: {e}"},
                                     status=500)

    try:
        rows, fleet_lim = await build_fleet()
    except Exception as e:
        print(f"  [usage] fleet ({provider}): {type(e).__name__}: {e}",
              flush=True)
        rows, fleet_lim = [], {}
    mine = [r for r in rows
            if (r.get("provider") or DEFAULT_PROVIDER) == provider]
    # Claude's 5h/7d come from the live statusline dumps. Codex's come from the
    # journals. Grok's weekly credits come from its billing log + session
    # context from signals.json (already on data["limits"]).
    if provider == DEFAULT_PROVIDER:
        limits = fleet_lim or data.get("limits") or {}
    else:
        limits = data.get("limits") or {}
    payload = {k: v for k, v in data.items() if k != "limits"}
    return web.json_response({
        **payload,
        "provider": provider,
        "limits": limits,
        "fleet": {"panes": len(mine),
                  "working": sum(1 for r in mine if r["state"] == "working"),
                  "live_cost": round(sum(r.get("cost") or 0 for r in mine), 2)},
        "age": int(now - computed_at),
        # Absolute epoch the data was computed at, so the client can tick the
        # freshness label off its own clock (same pattern as the fleet timers).
        "computed_at": computed_at,
    })


# ── history: every agent that has come through the fleet ────────────────────
# Claude: top-level ~/.claude/projects/<proj>/<session>.jsonl (subagent files
# live in subdirs and are skipped). Codex: ~/.codex/sessions rollouts. Grok:
# ~/.grok/sessions/<cwd>/<id>/. Mixed into one newest-first list; resume boots
# the matching CLI in that session's working dir (see api_resume).
_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
_hist_cache = {}      # path -> {"k":[mtime,size], "e":entry}
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,}$")
RESUME_ARGS = {
    "claude": "--resume {sid}",
    "codex":  "resume {sid}",
    "grok":   "--resume {sid}",
}


def _scan_history_file(path):
    try:
        lines = pathlib.Path(path).read_text(errors="replace").splitlines()
    except Exception:
        return None
    from datetime import datetime

    import cc_history as HIST
    cwd = None; model = None; first = last = None; prompts = 0
    itok = otok = cctok = crtok = 0; seen = set(); title = first_title = ""
    blurb = []; blurb_len = 0                # all user prompts, for search
    for ln in lines:
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if cwd is None and o.get("cwd"):
            cwd = o.get("cwd")
        typ = o.get("type")
        m = o.get("message") if isinstance(o.get("message"), dict) else {}
        # newest genuine user prompt → the card title. Where a session got to
        # says more about it than where it started: the opening line of a long
        # chat is usually "fix the build", which names every row in the list.
        if typ == "user" and isinstance(m, dict):
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content
                                   if isinstance(c, dict) and c.get("type") == "text")
            clean = human_prompt(content) if isinstance(content, str) else ""
            if clean:
                prompts += 1
                clean = clean.replace("\n", " ")
                title = clean[:120]
                if not first_title:
                    first_title = title
                if blurb_len < 1600:                 # bound the search text per session
                    take = clean[:300]
                    blurb.append(take); blurb_len += len(take)
        u = m.get("usage") if isinstance(m, dict) else None
        if isinstance(u, dict):
            mid = m.get("id")
            if not (mid and mid in seen):
                if mid:
                    seen.add(mid)
                itok += u.get("input_tokens", 0) or 0
                otok += u.get("output_tokens", 0) or 0
                cctok += u.get("cache_creation_input_tokens", 0) or 0
                crtok += u.get("cache_read_input_tokens", 0) or 0
            model = m.get("model") or model
        ts = o.get("timestamp")
        if ts:
            try:
                ep = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                if first is None or ep < first: first = ep
                if last is None or ep > last: last = ep
            except Exception:
                pass
    if last is None:
        return None
    # Skip our own headless summariser runs (claude -p labeler) — not fleet agents.
    if first_title.startswith("You are labeling a") and "coding session" in first_title:
        return None
    cost = HIST.model_cost(model or "unknown", itok, otok, cctok, crtok) if model else 0.0
    return {
        "session_id": os.path.basename(path)[:-6],   # strip .jsonl
        "title": title or "(no prompt)",
        "cwd": cwd or "",
        "project": os.path.basename((cwd or "").rstrip("/")) or HIST.project_of(path),
        "model": HIST.short_model(model) if model else "",
        "prompts": prompts,
        "tokens": itok + otok + cctok,
        "cost": round(cost, 2),
        "last": last, "first": first,
        # every user prompt (bounded), original case — lets search hit any prompt
        # in the session (not just the title) and show a highlighted snippet
        "search": " ".join(blurb)[:1600],
        "provider": "claude",
    }


def _claude_hist_cached(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    k = [st.st_mtime, st.st_size]
    c = _hist_cache.get(path)
    if c and c["k"] == k:
        return c["e"]
    e = _scan_history_file(path)
    _hist_cache[path] = {"k": k, "e": e}
    return e


def _grok_child_ids(dirs):
    """Session ids listed under each Grok session's subagents/ folder."""
    ids = set()
    for d in dirs:
        try:
            names = os.listdir(os.path.join(d, "subagents"))
        except OSError:
            continue
        ids.update(n for n in names if n and not n.startswith("."))
    return ids


def _build_history(limit=60):
    claude_files = glob.glob(os.path.join(_PROJECTS_DIR, "*", "*.jsonl"))  # top-level only
    out = []
    live = set(claude_files)
    for p in claude_files:
        e = _claude_hist_cached(p)
        if e:
            out.append(e)
    for pack in _iter_codex_packs()[0]:
        e = pack.get("hist") if pack else None
        if e and e.get("prompts"):
            out.append(e)
    grok_packs, grok_dirs = _iter_grok_packs()
    grok_children = _grok_child_ids(grok_dirs)
    for pack in grok_packs:
        e = pack.get("hist") if pack else None
        if not e or not e.get("prompts"):
            continue
        if e.get("session_id") in grok_children:
            continue
        out.append(e)
    for gone in [p for p in _hist_cache if p not in live]:
        _hist_cache.pop(gone, None)
    out.sort(key=lambda e: e["last"] or 0, reverse=True)
    kept, n = [], {}
    for e in out:
        p = e.get("provider") or DEFAULT_PROVIDER
        if n.get(p, 0) >= limit:
            continue
        n[p] = n.get(p, 0) + 1
        kept.append(e)
    return kept


# Stale-while-revalidate: the very first build reads every transcript (slow once),
# after that we always answer from the in-memory result instantly and rebuild in
# the background. The per-file _hist_cache means those rebuilds only re-read the
# handful of transcripts that actually changed, so they're cheap.
_history_result = {"at": 0, "data": None, "building": False}
_HISTORY_TTL = 8


async def _rebuild_history():
    try:
        _history_result["data"] = await asyncio.to_thread(_build_history)
        _history_result["at"] = time.time()
    except Exception as e:
        print(f"  [history] rebuild failed: {type(e).__name__}: {e}", flush=True)
    finally:
        _history_result["building"] = False


@guard
async def api_history(request):
    now = time.time()
    r = _history_result
    if r["data"] is None:
        # cold: build once synchronously so the first response has content
        try:
            r["data"] = await asyncio.to_thread(_build_history)
            r["at"] = now
        except Exception as e:
            print(f"  [history] failed: {type(e).__name__}: {e}", flush=True)
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)
    elif now - r["at"] > _HISTORY_TTL and not r["building"]:
        r["building"] = True
        asyncio.create_task(_rebuild_history())   # refresh behind the instant answer
    return web.json_response({"history": r["data"]})


def resume_cli_args(provider, session_id):
    """CLI args that reopen a session in its own client."""
    tmpl = RESUME_ARGS.get(provider) or RESUME_ARGS[DEFAULT_PROVIDER]
    return tmpl.format(sid=shlex.quote(session_id))


def _history_record(session_id, provider=None):
    """Look up a history row by id (and provider, when given). Never trust a
    client-supplied path — cwd and provider both come from the transcript."""
    if not _UUID_RE.match(session_id or ""):
        return None
    want = session_id.upper()
    want_p = (provider or "").strip().lower() or None
    for rows in (_history_result.get("data") or [], _build_history(limit=10000)):
        for e in rows:
            if e["session_id"].upper() != want:
                continue
            if want_p and (e.get("provider") or DEFAULT_PROVIDER) != want_p:
                continue
            return e
    return None


def _cwd_for_session(session_id):
    """Look up a session's working dir from its transcript — never trust a
    client-supplied path. Returns None if no such top-level transcript exists."""
    rec = _history_record(session_id)
    return (rec.get("cwd") or None) if rec else None


@writes("resume")
async def api_resume(request):
    """Resume a past session in a fresh iTerm pane, in that session's own
    working dir, running the CLI that originally owned it."""
    body = request.get("_body") or {}
    session_id = (body.get("session_id") or "").strip()
    provider = (body.get("provider") or "").strip().lower()
    rec = await asyncio.to_thread(_history_record, session_id, provider or None)
    if not rec:
        return web.json_response({"error": "no such session"}, status=404)
    cwd = rec.get("cwd") or ""
    provider = rec.get("provider") or DEFAULT_PROVIDER
    if not cwd or not os.path.isdir(cwd):
        return web.json_response(
            {"error": f"working dir is gone: {cwd or '(unknown)'}"}, status=409)
    if provider not in PROVIDERS:
        return web.json_response(
            {"error": f"unknown agent {provider!r}"}, status=400)
    if not os.path.isabs(agent_bin(provider)):
        return web.json_response(
            {"error": f"{provider} is not installed on this host"}, status=409)
    await APP.async_refresh()
    if provider == DEFAULT_PROVIDER:
        trust_dir(cwd)
    cmd, launch_dir = _pane_launcher(
        cwd, args=resume_cli_args(provider, session_id), provider=provider)
    try:
        tab = fleet_tab(APP)
        if tab is None:
            _discard_launcher(launch_dir)
            return web.json_response(
                {"error": "no iTerm window open to resume into"}, status=409)
        src, vertical = pick_grid_split(tab)
        sess = await src.async_split_pane(vertical=vertical, before=False,
                                          profile_customizations=_launch_profile(cmd))
    except Exception as e:
        _discard_launcher(launch_dir)
        return web.json_response(
            {"error": f"could not open pane: {type(e).__name__}: {e}"}, status=500)
    uuid = sess.session_id.upper()
    KNOWN_AGENTS[uuid] = provider
    await normalize_pane(sess)             # land at the canonical width from birth
    asyncio.create_task(_auto_trust(sess))
    print(f"  [resume] {provider} {session_id} → pane {uuid} in {cwd}", flush=True)
    return web.json_response(
        {"uuid": uuid, "session_id": session_id, "provider": provider})


# ── web push notifications ──────────────────────────────────────────────────
# A single background watcher polls the fleet and, when a pane finishes working
# (working → idle) or starts blocking on a prompt, sends a Web Push to every
# subscribed device — so you get a phone notification even with the app closed.
_VAPID_PRIV = str(HERE / "vapid_private.pem")
try:
    _VAPID_PUB = (HERE / "vapid_public.txt").read_text().strip()
except Exception:
    _VAPID_PUB = ""
_VAPID_SUB = os.environ.get("VAPID_SUB", "mailto:admin@example.com")
_SUBS_FILE = HERE / ".push_subs.json"


def _load_subs():
    try:
        return json.loads(_SUBS_FILE.read_text())
    except Exception:
        return []


def _save_subs():
    try:
        _SUBS_FILE.write_text(json.dumps(_push_subs))
    except Exception as e:
        print(f"  [push] save failed: {type(e).__name__}: {e}", flush=True)


_push_subs = _load_subs()


def _send_one(sub, payload):
    from pywebpush import WebPushException, webpush
    try:
        webpush(subscription_info=sub, data=json.dumps(payload),
                vapid_private_key=_VAPID_PRIV,
                vapid_claims={"sub": _VAPID_SUB}, timeout=10)
        return True
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            return None                     # subscription is dead — prune it
        print(f"  [push] send error: {e}", flush=True)
        return False
    except Exception as e:
        print(f"  [push] send error: {type(e).__name__}: {e}", flush=True)
        return False


async def _push_all(payload):
    global _push_subs
    if not _push_subs or not _VAPID_PUB:
        return
    dead = []
    for sub in list(_push_subs):
        r = await asyncio.to_thread(_send_one, sub, payload)
        if r is None:
            dead.append(sub.get("endpoint"))
    if dead:
        _push_subs = [s for s in _push_subs if s.get("endpoint") not in dead]
        _save_subs()


@guard
async def api_vapid(request):
    return web.json_response({"key": _VAPID_PUB, "enabled": bool(_VAPID_PUB),
                              "subs": len(_push_subs)})


@writes("push.subscribe")
async def api_push_subscribe(request):
    global _push_subs
    sub = request.get("_body") or {}
    if not sub.get("endpoint"):
        return web.json_response({"error": "bad subscription"}, status=400)
    _push_subs = [s for s in _push_subs if s.get("endpoint") != sub["endpoint"]]
    _push_subs.append(sub)
    _save_subs()
    return web.json_response({"ok": True, "subs": len(_push_subs)})


@writes("push.unsubscribe")
async def api_push_unsubscribe(request):
    global _push_subs
    ep = (request.get("_body") or {}).get("endpoint")
    _push_subs = [s for s in _push_subs if s.get("endpoint") != ep]
    _save_subs()
    return web.json_response({"ok": True, "subs": len(_push_subs)})


@writes("push.test")
async def api_push_test(request):
    await _push_all({"title": "The Yard", "tag": "test",
                     "body": "Notifications are on. You'll hear when a session needs you."})
    return web.json_response({"ok": True, "subs": len(_push_subs)})


# Per-uuid notifier bookkeeping. The raw `state` a pane reports flaps: a Claude
# session bounces working↔idle many times inside one task (subagents, repeated Stop
# events), and a momentary fleet-file read miss makes read_fleet_files() default to
# "idle" (server.py ~L445) even while work continues. Firing on every raw edge spams
# "done" for a chat where nothing actually happened. So we debounce: a raw state must
# hold for CONFIRM_SECS before it becomes the *confirmed* state, and we only notify on
# confirmed transitions — never on the raw flapping.
_notify_state = {}      # uuid -> dict (see _new_track)

_POLL_SECS      = 2.5   # fleet poll interval
_CONFIRM_SECS   = 8.0   # a raw state must persist this long to be believed (~3 polls)
_PROMPT_CONFIRM = 4.0   # prompts are stable while blocking; confirm faster
_DONE_COOLDOWN  = 30.0  # backstop: never re-fire "done" for a uuid within this window
_DROP_GRACE     = 45.0  # keep debounce state this long after a uuid stops being seen,
                        # so a transient stale/missing file doesn't reset the machine


def _new_track(now, st, has_prompt):
    return {
        "raw": st, "raw_since": now, "confirmed": st,   # working/idle debounce
        "praw": has_prompt, "praw_since": now, "prompt_conf": has_prompt,
        "prompt_sent": False,       # rising-edge guard for the current prompt block
        "armed": True,              # may a "done" fire? re-armed by a confirmed working
        "last_done": 0.0,           # when we last pushed "done" (cooldown backstop)
        "seen": now,                # last poll this uuid appeared in the fleet
    }


async def _notify_watcher():
    """Watch the fleet; push when a pane finishes or starts needing input.

    Notifications fire off *confirmed* (debounced) state, not the raw per-poll
    reading, so a flapping session no longer spams "done" for the same chat."""
    print(f"  [notify] watcher running (push {'ON' if _VAPID_PUB else 'DISABLED'}, "
          f"{len(_push_subs)} subscriber(s))", flush=True)
    await asyncio.sleep(5)
    while True:
        try:
            rows, _ = await build_fleet()
            now = time.time()
            live = set()
            for r in rows:
                uuid = r.get("uuid"); live.add(uuid)
                st = r.get("state"); prm = r.get("prompt"); has_prompt = bool(prm)
                name = r.get("name") or "A session"
                t = _notify_state.get(uuid)
                if t is None:
                    # First sighting — seed confirmed = current, never fire on startup.
                    _notify_state[uuid] = _new_track(now, st, has_prompt)
                    continue
                t["seen"] = now

                # ── working/idle debounce ──────────────────────────────────
                if st != t["raw"]:
                    t["raw"] = st; t["raw_since"] = now      # raw changed — restart clock
                if st == t["raw"] and now - t["raw_since"] >= _CONFIRM_SECS \
                        and st != t["confirmed"]:
                    prior = t["confirmed"]
                    t["confirmed"] = st
                    if st == "working":
                        t["armed"] = True                    # re-arm for the next finish
                    elif prior == "working" and st == "idle" and not has_prompt \
                            and t["armed"] and now - t["last_done"] >= _DONE_COOLDOWN:
                        t["armed"] = False                   # one "done" per work cycle
                        t["last_done"] = now
                        await _push_all({
                            "title": "Session finished",
                            "body": f"{name} is done — ready for your next prompt.",
                            "tag": f"done-{uuid}", "uuid": uuid})

                # ── prompt (needs-input) debounce ──────────────────────────
                if has_prompt != t["praw"]:
                    t["praw"] = has_prompt; t["praw_since"] = now
                if has_prompt == t["praw"] and now - t["praw_since"] >= _PROMPT_CONFIRM \
                        and has_prompt != t["prompt_conf"]:
                    t["prompt_conf"] = has_prompt
                    if not has_prompt:
                        t["prompt_sent"] = False             # block cleared — re-arm
                    elif not t["prompt_sent"]:
                        t["prompt_sent"] = True
                        q = (prm.get("question") or "").strip()
                        if not q:
                            opts = [o.get("label", "") for o in prm.get("options") or []]
                            q = " / ".join(o for o in opts[:3] if o) or "waiting on a choice"
                        await _push_all({
                            "title": f"{name} needs you",
                            "body": q[:180],
                            "tag": f"prompt-{uuid}", "uuid": uuid})

            # Drop debounce state only after a grace window of not being seen, so a
            # one-poll stale/missing fleet file doesn't reset the machine and let the
            # next reappearance re-fire.
            for u in [u for u, t in _notify_state.items()
                      if u not in live and now - t["seen"] > _DROP_GRACE]:
                _notify_state.pop(u, None)
        except Exception as e:
            print(f"  [notify] {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(_POLL_SECS)


async def serve_sw(request):
    # Served from root so its scope covers the whole app.
    resp = web.FileResponse(HERE / "static" / "sw.js")
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ── watching a pane ────────────────────────────────────────────────────────
# grow_pane only counts viewers. Nobody is maximized: Grok /minimal reprints
# on resize the same way Claude and Codex do. ungrow_pane still restores a
# leftover maximize after _RESTORE_GRACE so a reconnect does not flash the grid.
_GROWN = None      # (uuid, prior active session id, window id) or None
_GROW_LOCK = asyncio.Lock()   # one viewer reshapes the layout at a time
_WATCHERS = {}                # uuid -> open pane websockets watching it
_UNGROW_TRIES = 0             # gentle restores that found the wrong key window
_RESTORE_GRACE = 1.0          # seconds. Phone reconnect is 700ms; grace prevents maximize-flash on reconnect.
_RESTORE_AFTER = {}           # uuid -> monotonic deadline while grace is running
_GROW_FOCUS_TRIES = 0         # cap window-activate retries so janitor cannot steal focus forever
_GROW_FOCUS_RETRY_AT = 0.0    # monotonic time when a fresh activate burst is allowed
_GROW_TRIED = set()           # uuids we already issued a maximize for this watch session


def _maximize_id():
    """The menu API takes the identifier string, not the enum member. Resolved on
    use, not at import: the test stub for iterm2 has no menu tree."""
    return iterm2.MainMenu.View.MAXIMIZE_ACTIVE_PANE.value.identifier


def _locate(uuid):
    """The window, tab and session for a pane uuid — (None, None, None) if gone."""
    want = (uuid or "").upper()
    for w in (APP.terminal_windows if APP else []):
        for t in w.tabs:
            for sess in t.all_sessions:
                if sess.session_id.upper() == want:
                    return w, t, sess
    return None, None, None


def _is_key(w):
    cur = APP.current_window if APP else None
    return cur is not None and w is not None and cur.window_id == w.window_id


async def _maximized():
    st = await iterm2.MainMenu.async_get_menu_item_state(
        CONN, _maximize_id())
    return bool(getattr(st, "checked", False))


async def _toggle_maximize():
    print("  [grow] toggle Maximize Active Pane", flush=True)
    await iterm2.MainMenu.async_select_menu_item(CONN, _maximize_id())


def _tab_is_maximized(tab):
    """True when iTerm has buried tab-mates behind one visible pane."""
    if tab is None:
        return False
    mini = getattr(tab, "minimized_sessions", None) or []
    vis = getattr(tab, "sessions", None) or []
    return len(mini) > 0 and len(vis) == 1


async def _grow(uuid, steal=True):
    """Maximize a watched Grok pane in its tab. Caller holds _GROW_LOCK.

    steal=True (a newly opened chat) may toggle once. steal=False (janitor)
    only adopts an already-maximized layout — it never hits the menu. Re-reading
    the menu's checked flag every POLL and toggling when it disagreed is what
    sent iTerm in and out of the maximized pane (looks like fullscreen flicker)."""
    global _GROWN, _GROW_FOCUS_TRIES, _GROW_FOCUS_RETRY_AT
    uuid = (uuid or "").upper()
    if _GROWN and _GROWN[0] == uuid:
        return
    if _GROWN:
        if not steal:
            return
        await _ungrow()
        if _GROWN:            # the last one is still maximized — never stack a
            return            # second, or nothing can be put back
    w, t, sess = _locate(uuid)
    if sess is None or len(t.all_sessions) < 2:
        return
    try:
        prior = t.active_session_id
        if _tab_is_maximized(t) and t.sessions[0].session_id.upper() == uuid:
            _GROWN = (uuid, prior, w.window_id)
            _GROW_FOCUS_TRIES = 0
            return
        if not steal:
            return
        await sess.async_activate(select_tab=True, order_window_front=False)
        if not _is_key(w):
            now = time.monotonic()
            if _GROW_FOCUS_TRIES >= 3 and now < _GROW_FOCUS_RETRY_AT:
                return
            if _GROW_FOCUS_TRIES >= 3:
                _GROW_FOCUS_TRIES = 0
            await w.async_activate()
            if APP is not None:
                try:
                    await APP.async_refresh()
                except Exception:
                    pass
            if not _is_key(w):
                _GROW_FOCUS_TRIES += 1
                if _GROW_FOCUS_TRIES >= 3:
                    _GROW_FOCUS_RETRY_AT = now + 5.0
                return
        if _tab_is_maximized(t) and t.sessions[0].session_id.upper() == uuid:
            _GROWN = (uuid, prior, w.window_id)
            _GROW_FOCUS_TRIES = 0
            return
        if not await _maximized():
            await _toggle_maximize()
        _GROWN = (uuid, prior, w.window_id)
        _GROW_FOCUS_TRIES = 0
    except Exception as e:
        print(f"  [grow] {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()


async def _ungrow():
    """Put the tab back the way the viewer found it. Caller holds _GROW_LOCK.

    _GROWN is only cleared once the pane really is un-maximized: while it stands,
    the tab-mates are invisible to iTerm's API, so a restore that quietly failed
    would leave every other agent unreachable from the yard."""
    global _GROWN, _UNGROW_TRIES
    if not _GROWN:
        return
    uuid, prior, _wid = _GROWN
    w, t, sess = _locate(uuid)
    if sess is None:               # pane or window gone; nothing of ours to undo
        _GROWN, _UNGROW_TRIES = None, 0
        return
    try:
        if not _tab_is_maximized(t):
            _GROWN, _UNGROW_TRIES = None, 0
            return
        await sess.async_activate(select_tab=True, order_window_front=False)
        if not _is_key(w):
            # Toggling now would maximize whatever the Mac is focused on instead.
            await w.async_activate()
            if APP is not None:
                try:
                    await APP.async_refresh()
                except Exception:
                    pass
            if not _is_key(w):
                _UNGROW_TRIES += 1
                return
        if _tab_is_maximized(t) or await _maximized():
            await _toggle_maximize()
            if APP is not None:
                try:
                    await APP.async_refresh()
                except Exception:
                    pass
            w, t, sess = _locate(uuid)
            if t is not None and _tab_is_maximized(t):
                _UNGROW_TRIES += 1
                return
        _GROWN, _UNGROW_TRIES = None, 0
        if prior and prior.upper() != uuid.upper():
            _w2, _t2, back = _locate(prior)
            if back is not None:
                await back.async_activate(select_tab=True, order_window_front=False)
    except Exception as e:
        _UNGROW_TRIES += 1
        print(f"  [grow] restore {type(e).__name__}: {e}", flush=True)


async def grow_pane(uuid):
    """Register a viewer. Never maximize — Grok /minimal reprints on resize
    the same way Claude and Codex do."""
    uuid = (uuid or "").upper()
    _RESTORE_AFTER.pop(uuid, None)
    _WATCHERS[uuid] = _WATCHERS.get(uuid, 0) + 1


async def ungrow_pane(uuid):
    """Release one watcher. Only the pane's last viewer puts the tab back — an
    older socket closing must not undo the pane a newer one just opened.
    Restore waits _RESTORE_GRACE so a phone reconnect does not flash the grid."""
    uuid = (uuid or "").upper()
    left = _WATCHERS.get(uuid, 1) - 1
    if left > 0:
        _WATCHERS[uuid] = left
        return
    _WATCHERS.pop(uuid, None)
    _GROW_TRIED.discard(uuid)
    _RESTORE_AFTER[uuid] = time.monotonic() + _RESTORE_GRACE
    try:
        if _RESTORE_GRACE:
            await asyncio.sleep(_RESTORE_GRACE)
        async with _GROW_LOCK:
            if _WATCHERS.get(uuid, 0) == 0 and _GROWN and _GROWN[0] == uuid:
                await _ungrow()
    finally:
        _RESTORE_AFTER.pop(uuid, None)


async def _maybe_grow(uuid, provider=None):
    """No-op. Nobody is maximized for a phone watcher."""
    return


def _orphan_maximize():
    """A tab of ours left maximized with no record of who did it: the server
    restarted while a phone was watching, or someone hit ⇧⌘⏎ on the Mac and
    walked away. Returns (uuid, window id) of the visible pane, or None."""
    for w in (APP.terminal_windows if APP else []):
        for t in w.tabs:
            if not t.minimized_sessions or len(t.sessions) != 1:
                continue
            if not any(s.session_id.upper() in KNOWN_AGENTS
                       for s in t.all_sessions):
                continue                  # not a tab we manage
            return t.sessions[0].session_id.upper(), w.window_id
    return None


async def _grow_tick():
    """One janitor pass. Holds _GROW_LOCK for the whole sweep.

    Never grow a watched Grok pane. Restore leftover `_GROWN` once unwatched
    (respect grace). Adopt and restore any orphan maximize.
    """
    global _GROWN
    async with _GROW_LOCK:
        if _GROWN:
            uuid = _GROWN[0]
            deadline = _RESTORE_AFTER.get(uuid)
            if deadline is not None and time.monotonic() < deadline:
                return
            await _ungrow()
            return
        orphan = _orphan_maximize()
        if orphan:
            if not _GROWN:
                _GROWN = (orphan[0], None, orphan[1])
                print(f"  [grow] adopting stray maximize on "
                      f"{orphan[0][:8]}", flush=True)
                await _ungrow()


async def _grow_janitor():
    """Restore leftover maximizes; never grow a watched pane."""
    while True:
        await asyncio.sleep(POLL)
        try:
            await _grow_tick()
        except Exception:
            traceback.print_exc()


async def ws_pane(request):
    if not authed(request):
        return web.json_response({"error": "locked"}, status=401)
    uuid = request.match_info["uuid"].upper()
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    last = None
    hist_end = None                # absolute line after the scrollback we sent
    missing = 0                    # consecutive polls the pane was not listed
    await grow_pane(uuid)          # count the viewer
    try:
        while not ws.closed:
            s = (await all_sessions()).get(uuid)
            if not s:
                # all_sessions() sees minimized panes too, so a maximized
                # tab-mate no longer hides this one. What is left is a pane
                # mid-relayout (a split or a maximize toggle in flight) or one
                # that really ended — give it a few polls before saying so.
                missing += 1
                if missing < 5:
                    await asyncio.sleep(POLL)
                    continue
                await ws.send_json({"gone": True})
                break
            missing = 0
            job = await s.async_get_variable("jobName") or ""
            if hist_end is None:
                # scrollback is expensive and rarely changes at the top; ship
                # the tail once so the client can render the conversation …
                hist, hist_end = await pane_history(s)
                payload = {"history": hist, "cols": await pane_cols(s)}
                await ws.send_json(payload)
            else:
                # … then only the rows that have since scrolled off the screen
                more, hist_end = await pane_history(s, since=hist_end)
                if more:
                    await ws.send_json({"history_add": more})
            txt = await pane_text(s)
            if txt != last:                      # only push on change
                last = txt
                provider = pane_provider(uuid, job, txt)
                payload = {"text": txt, "cols": await pane_cols(s),
                           "prompt": detect_prompt(txt, provider, uuid),
                           "suggest": detect_input(txt, provider)}
                await ws.send_json(payload)
            await asyncio.sleep(POLL)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        await ungrow_pane(uuid)
        if not ws.closed:
            await ws.close()
    return ws


_WS_FLEET = set()   # live /ws/fleet sockets, for the load page's "watchers" count


async def ws_fleet(request):
    peer = request.remote
    if not authed(request):
        print(f"  [ws/fleet] {peer} REJECTED — no unlocked session", flush=True)
        return web.json_response({"error": "locked"}, status=401)
    print(f"  [ws/fleet] {peer} connected", flush=True)
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    _WS_FLEET.add(ws)
    last = None
    try:
        while not ws.closed:
            rows, limits = await build_fleet()
            pending = vault.pending_count()
            blob = json.dumps([rows, limits, pending], sort_keys=True)
            if blob != last:
                first = last is None
                last = blob
                await ws.send_json({"sessions": rows, "limits": limits,
                                    "pending": pending})
                if first:
                    print(f"  [ws/fleet] {peer} first payload sent "
                          f"({len(rows)} panes, {len(blob)} bytes)", flush=True)
            await asyncio.sleep(1.0)
    except (asyncio.CancelledError, ConnectionResetError):
        print(f"  [ws/fleet] {peer} disconnected", flush=True)
    except Exception as e:
        print(f"  [ws/fleet] {peer} ERROR {type(e).__name__}: {e}", flush=True)
    finally:
        _WS_FLEET.discard(ws)
        if not ws.closed:
            await ws.close()
    return ws


# ── agent auth broker ────────────────────────────────────────────────────────
# Two audiences, two trust models:
#   * The PHONE (owner) reaches /api/integrations|requests|grants — passkey-gated,
#     same as every other write, via @guard/@writes.
#   * An AGENT (a spawned pane) reaches /agent/* — a local program, so it proves
#     identity with a per-pane capability token, and the endpoints only ever
#     answer on loopback. Crucially, a secret is released only when the owner has
#     already created a grant from the phone; the pane token authorizes nothing
#     on its own.
PANE_TOKENS = {}          # broker_token -> pane uuid, minted at spawn (ephemeral)
DEVICE_FLOWS = {}         # flow_id -> {device_code, interval, provider, ...}
GITHUB_CLIENT_ID = os.environ.get("DISPATCH_GITHUB_CLIENT_ID", "")


_LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


async def _local_agent(request):
    """Resolve an /agent/* caller to (uuid, body), or (None, err_response).

    Two hard gates before the token is even consulted: the call must arrive on
    loopback, and it must NOT carry the X-Forwarded-* headers that `tailscale
    serve` stamps on the phone UI's traffic. So the broker is unreachable from
    the tailnet by construction — only a process on this Mac can speak to it.
    Then the per-pane token must map to a live pane.
    """
    if request.headers.get("X-Forwarded-For") or request.headers.get("X-Forwarded-Host") \
            or (request.remote or "") not in _LOOPBACK:
        return None, web.json_response({"error": "not local"}, status=403)
    try:
        b = await request.json()
    except Exception:
        b = {}
    uuid = PANE_TOKENS.get(b.get("token") or "")
    if not uuid:
        return None, web.json_response({"error": "unknown agent"}, status=401)
    return (uuid, b), None


async def agent_request(request):
    """An agent asks the owner for a service credential."""
    got, err = await _local_agent(request)
    if err:
        return err
    uuid, b = got
    service = (b.get("service") or "").strip()
    if not service:
        return web.json_response({"error": "no service"}, status=400)
    rid = vault.add_request(uuid, service, b.get("reason", ""))
    auth.audit(request, "integ.request", {"uuid": uuid, "service": service, "req": rid})
    print(f"  [broker] {uuid} requests '{service}' (req {rid})", flush=True)
    return web.json_response({"ok": True, "req": rid, "status": "pending"})


async def agent_fetch(request):
    """An agent redeems a granted credential — the one place a secret is emitted."""
    got, err = await _local_agent(request)
    if err:
        return err
    uuid, b = got
    service = (b.get("service") or "").strip()
    rel = vault.release(uuid, service)
    if not rel:
        rid = vault.add_request(uuid, service, "auto — fetch before grant")
        return web.json_response({"status": "pending", "req": rid}, status=202)
    env_var, secret, cred_id, last4 = rel
    auth.audit(request, "integ.release",
               {"uuid": uuid, "service": service, "cred": cred_id, "last4": last4})
    print(f"  [broker] released '{service}' (…{last4}) to {uuid}", flush=True)
    return web.json_response({"env_var": env_var, "secret": secret})


async def agent_list(request):
    """What this pane currently holds — no secrets, just shape."""
    got, err = await _local_agent(request)
    if err:
        return err
    uuid, _b = got
    mine = [g for g in vault.list_grants() if g["uuid"] == uuid]
    return web.json_response({"grants": mine})


# ── phone-side: integrations (the vault) ─────────────────────────────────────
@guard
async def api_integrations(request):
    return web.json_response({"integrations": vault.list_creds(),
                              "providers": vault.providers_public()})


@writes("integ.add")
async def api_integrations_add(request):
    b = request.get("_body") or {}
    provider = (b.get("provider") or "custom").strip()
    secret = (b.get("secret") or "").strip()
    if not secret:
        return web.json_response({"error": "no token"}, status=400)
    expires = b.get("expires")
    if provider != "custom" and not expires:
        # A pasted long-lived token with no expiry is exactly what we don't want
        # sitting in the vault forever. Require the owner to state one.
        return web.json_response({"error": "expiry required"}, status=400)
    cid = vault.add_cred(provider, b.get("label"), secret,
                         env_var=b.get("env_var"), scopes=b.get("scopes") or [],
                         expires=expires)
    return web.json_response({"ok": True, "id": cid,
                              "integration": vault.redact(cid, vault.get_cred(cid))})


@writes("integ.scope")
async def api_integration_scope(request):
    cid = request.match_info["id"]
    b = request.get("_body") or {}
    ok = vault.update_cred(cid, label=b.get("label"), scopes=b.get("scopes"),
                           expires=b.get("expires"), env_var=b.get("env_var"),
                           secret=b.get("secret"))
    if not ok:
        return web.json_response({"error": "no such integration"}, status=404)
    return web.json_response({"ok": True,
                              "integration": vault.redact(cid, vault.get_cred(cid))})


@writes("integ.delete")
async def api_integration_delete(request):
    cid = request.match_info["id"]
    if not vault.delete_cred(cid):
        return web.json_response({"error": "no such integration"}, status=404)
    return web.json_response({"ok": True})


# ── phone-side: GitHub device flow ───────────────────────────────────────────
def _http_json(url, data):
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


@writes("integ.device")
async def api_device_start(request):
    if not GITHUB_CLIENT_ID:
        return web.json_response(
            {"error": "device flow needs DISPATCH_GITHUB_CLIENT_ID set; "
                      "paste a token instead"}, status=400)
    b = request.get("_body") or {}
    scopes = " ".join(b.get("scopes") or ["repo", "read:org", "workflow"])
    try:
        d = await asyncio.to_thread(
            _http_json, "https://github.com/login/device/code",
            {"client_id": GITHUB_CLIENT_ID, "scope": scopes})
    except Exception as e:
        return web.json_response({"error": f"github: {e}"}, status=502)
    flow_id = secrets.token_urlsafe(8)
    DEVICE_FLOWS[flow_id] = {"device_code": d["device_code"],
                             "interval": d.get("interval", 5),
                             "scopes": b.get("scopes") or [],
                             "label": b.get("label") or "GitHub"}
    return web.json_response({"flow": flow_id, "user_code": d["user_code"],
                              "verification_uri": d["verification_uri"],
                              "expires_in": d.get("expires_in", 900)})


@writes("integ.device.poll")
async def api_device_poll(request):
    b = request.get("_body") or {}
    flow = DEVICE_FLOWS.get(b.get("flow"))
    if not flow:
        return web.json_response({"error": "unknown flow"}, status=404)
    try:
        d = await asyncio.to_thread(
            _http_json, "https://github.com/login/oauth/access_token",
            {"client_id": GITHUB_CLIENT_ID, "device_code": flow["device_code"],
             "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    except Exception as e:
        return web.json_response({"error": f"github: {e}"}, status=502)
    if d.get("error") == "authorization_pending":
        return web.json_response({"status": "pending"})
    if d.get("error") == "slow_down":
        return web.json_response({"status": "pending", "slow_down": True})
    tok = d.get("access_token")
    if not tok:
        return web.json_response({"status": "error", "error": d.get("error", "no token")},
                                 status=400)
    cid = vault.add_cred("github", flow["label"], tok, scopes=flow["scopes"],
                         expires=None)                 # GitHub user tokens self-expire
    DEVICE_FLOWS.pop(b.get("flow"), None)
    return web.json_response({"status": "ok", "id": cid,
                              "integration": vault.redact(cid, vault.get_cred(cid))})


# ── phone-side: requests + grants ────────────────────────────────────────────
@guard
async def api_requests(request):
    return web.json_response({"requests": vault.pending_requests()})


@writes("grant.add")
async def api_request_approve(request):
    rid = request.match_info["id"]
    req = vault.get_request(rid)
    if not req:
        return web.json_response({"error": "no such request"}, status=404)
    b = request.get("_body") or {}
    cred_id = b.get("cred_id")
    if not cred_id or not vault.get_cred(cred_id):
        return web.json_response({"error": "pick a credential"}, status=400)
    gid = vault.add_grant(req["uuid"], cred_id, scopes=b.get("scopes") or [],
                          expires=b.get("expires"))
    vault.set_request_status(rid, "granted")
    return web.json_response({"ok": True, "grant": gid})


@writes("grant.deny")
async def api_request_deny(request):
    rid = request.match_info["id"]
    if not vault.get_request(rid):
        return web.json_response({"error": "no such request"}, status=404)
    vault.set_request_status(rid, "denied")
    return web.json_response({"ok": True})


@guard
async def api_grants(request):
    return web.json_response({"grants": vault.list_grants()})


@writes("grant.revoke")
async def api_grant_revoke(request):
    if not vault.revoke_grant(request.match_info["id"]):
        return web.json_response({"error": "no such grant"}, status=404)
    return web.json_response({"ok": True})


# ── boot ───────────────────────────────────────────────────────────────────
# The launching shell's PATH is not to be trusted: iTerm/GUI launches and headless
# SSH both drop /usr/local/bin, where `tailscale` lives. Resolve the binary once,
# by hand, so every device sees its own tailnet identity no matter how it started.
def _ts_bin():
    import shutil
    onpath = shutil.which("tailscale")
    if onpath:
        return onpath
    for p in ("/usr/local/bin/tailscale",
              "/opt/homebrew/bin/tailscale",
              "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if os.path.exists(p):
            return p
    return "tailscale"


_TAILSCALE = _ts_bin()


# The tailscale CLI is slow to answer over the tailnet (seconds on some hosts),
# and /api/ping fans out to it ~4× per request — enough to blow past the swapper's
# probe timeout so a live peer reads "offline". status/serve state barely moves, so
# cache each arg-set briefly: the first call pays the cost, the rest are instant.
_TS_CACHE = {}          # args tuple -> (expiry, value)
_TS_TTL = 15


def _ts_json(*args):
    import shlex
    now = time.time()
    hit = _TS_CACHE.get(args)
    if hit and hit[0] > now:
        return hit[1]
    cmd = " ".join(shlex.quote(a) for a in (_TAILSCALE, *args))
    try:
        val = json.loads(os.popen(f"{cmd} 2>/dev/null").read())
    except Exception:
        val = {}
    if val:             # cache only real answers — never pin an empty/failed read
        _TS_CACHE[args] = (now + _TS_TTL, val)
    return val


def ts_name():
    """The https host `tailscale serve` is publishing this port on, if any."""
    st = _ts_json("serve", "status", "--json")
    for hostport, conf in (st.get("Web") or {}).items():
        for _, h in (conf.get("Handlers") or {}).items():
            if str(h.get("Proxy", "")).endswith(f":{PORT}"):
                return hostport.split(":")[0]
    return ""


# ── tailnet device map ──────────────────────────────────────────────────────
# The swapper needs to know its siblings: other Macs on the tailnet that could
# be running their own copy of Dispatch. We DON'T proxy across them — each host
# is its own security origin (its own passkey, its own cookie), so the swapper
# just navigates the browser to the peer's origin. All we expose here is the
# roster; auth still happens fresh on whichever host you land on.
def ts_status():
    return _ts_json("status", "--json")


def ts_self_host():
    """This device's own tailnet DNS name (no trailing dot), or ''."""
    return ((ts_status().get("Self") or {}).get("DNSName") or "").rstrip(".")


def ts_serve_origin():
    """The public https origin `tailscale serve` publishes THIS server's PORT on,
    e.g. https://host or https://host:8443 (port omitted when it's 443). '' when
    serve isn't fronting us yet. This is the origin the swapper must navigate to,
    port and all — a device may live at :8443 because its root is taken."""
    st = _ts_json("serve", "status", "--json")
    for hostport, conf in (st.get("Web") or {}).items():
        for _, h in (conf.get("Handlers") or {}).items():
            if str(h.get("Proxy", "")).endswith(f":{PORT}"):
                host, _, port = hostport.partition(":")
                return f"https://{host}" if port in ("", "443") else f"https://{host}:{port}"
    return ""


# Peers whose Dispatch does NOT live at their tailnet root (e.g. BigMac serves it
# at :8443 because OpenClaw owns the root). Optional JSON map: {host: base_url}.
# Anything not listed is assumed to sit at https://<host>.
PEERS_FILE = HERE / ".peers.json"
WAKE_FILE = HERE / ".wake_targets.json"
ACTIVITY_FILE = HERE / ".last_activity"
WAKE_SCRIPT = HERE / "wake-macbookpro.sh"


def load_peers():
    try:
        return json.loads(PEERS_FILE.read_text())
    except Exception:
        return {}


def wake_targets():
    """Tailnet hosts we know how to wake: {host: {ip, mac, ...}}.

    Written out of band -- the LAN address and MAC a magic packet needs are not
    anything Tailscale will tell us about a node that is already asleep. A
    missing or malformed file just means nothing is wakeable.
    """
    try:
        d = json.loads(WAKE_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def self_origin():
    return ts_serve_origin() or (f"https://{ts_self_host()}" if ts_self_host() else "")


def tailnet_macs():
    """Mac devices on the tailnet, self first, each with the origin the swapper
    should navigate to. Dispatch is an iTerm/macOS app, so phones and Linux boxes
    are filtered out — they can't host a backend."""
    st = ts_status()
    peers = load_peers()
    out = []

    def add(node, is_self):
        if (node.get("OS") or "").lower() != "macos":
            return
        host = (node.get("DNSName") or "").rstrip(".")
        if not host:
            return
        url = self_origin() if is_self else (peers.get(host) or f"https://{host}")
        out.append({"name": node.get("HostName") or host.split(".")[0],
                    "host": host, "url": url, "os": node.get("OS") or "",
                    "online": True if is_self else bool(node.get("Online")),
                    "wakeable": (not is_self) and host in wake_targets(),
                    "sleepable": is_self and host in wake_targets(),
                    "self": is_self})

    add(st.get("Self") or {}, True)
    for p in (st.get("Peer") or {}).values():
        add(p, False)
    return out


async def api_devices(request):
    """The tailnet Macs the swapper can hop between, plus which one is us."""
    if not authed(request):
        return web.json_response({"error": "locked"}, status=401)
    return web.json_response({"self": ts_self_host(), "devices": tailnet_macs()})


async def _drive_wake_script(request, mode, timeout):
    """Shared body of /api/wake and /api/sleep.

    Both are the same shape: authenticate, check the host is one we actually
    know how to manage, then let wake-macbookpro.sh do the work. It is what
    knows the whole dance, and writing any of it twice would let the two drift.

    Only hosts already named in the wake-targets file can be asked for, so this
    cannot be turned into a packet cannon aimed at arbitrary addresses.
    """
    if not authed(request):
        return web.json_response({"error": "locked"}, status=401)
    try:
        d = await request.json()
    except Exception:
        d = {}
    host = (d.get("host") or "").rstrip(".")
    if host not in wake_targets():
        return web.json_response({"error": "not a wake target"}, status=400)
    if not WAKE_SCRIPT.exists():
        return web.json_response({"error": "wake script missing"}, status=500)
    what = mode.lstrip("-")

    proc = await asyncio.create_subprocess_exec(
        str(WAKE_SCRIPT), mode,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "WAKE_HOST": host})
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return web.json_response({"error": f"{what} timed out"}, status=504)
    log = (out or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        # The script is deliberately loud about *why* it failed (not pinned, no
        # sudoers rule, never came up); pass its last word through to the UI.
        return web.json_response(
            {"error": log.splitlines()[-1] if log else f"{what} failed", "log": log},
            status=502)
    return web.json_response({"ok": True, "log": log})


async def api_wake(request):
    """Wake a sleeping peer so the swapper has somewhere to hop to.

    A magic packet on its own only buys a few seconds of dark wake -- long
    enough to answer a ping, nowhere near long enough to use -- so the script
    also holds the machine up and confirms Dispatch came back with it.
    """
    return await _drive_wake_script(request, "--wake", 150)


async def api_sleep(request):
    """Put a peer back to sleep -- the other half of the wake button.

    Waking pins the machine awake, which on a laptop means it sits there
    spending battery until something says otherwise. This hands sleep back and
    sleeps it now rather than waiting out an idle timer.
    """
    return await _drive_wake_script(request, "--sleep", 90)


# ── background load sampler ─────────────────────────────────────────────────
# `top -l 2 -n 0 -s 1` takes ~2 s to settle on a real (non-instantaneous) CPU
# reading, far too slow to run per /api/sysinfo request. Sample it — plus GPU,
# memory pressure, swap and thermal state — on a 10 s timer instead, and let
# the request handler read the cached snapshot for free.
_LOAD = {}     # cpu_pct/cpu_user/cpu_sys/gpu_pct/mem_free_pct/swap_used/
               # swap_total/thermal/cpu_limit/temp_c/temp_cell_c/bat_cycles/
               # bat_health/sampled — any key may be None


# ── background shells ───────────────────────────────────────────────────────
# A `run_in_background` Bash call keeps running after the turn that started it,
# and nothing on the pane says so — the transcript scrolls on and the agent looks
# idle while a dev server or a build is still alive underneath. Two facts have to
# be joined to know that honestly:
#
#   * WHICH shells were backgrounded. Only the transcript says. A foreground tool
#     call gets an identical wrapper process and an identical .output file, so the
#     process table cannot tell them apart — but the tool result that launched a
#     background one announces its id in so many words.
#   * WHETHER one is still running. An open write fd on its .output file is the
#     only trustworthy signal: a sleeping shell (a server waiting for requests,
#     a `sleep`) writes nothing for minutes, so file mtime says "finished" about
#     a task that is very much alive.
#
# So: ids from the transcript, liveness from one lsof over every task file at
# once, joined on the 10s sampler rather than per fleet frame.
_BG_OUT = f"/private/tmp/claude-{os.getuid()}/*/*/tasks/*.output"
_BG_RE = re.compile(r"running in background with ID:\s*([A-Za-z0-9_-]{4,32})")
_BG_SEEN = {}   # transcript path -> {"off": bytes scanned, "ids": [task id, ...]}
_BG = {}        # transcript path -> how many of its background shells are alive
_BG_KEEP = 200  # ids remembered per session; a finished one can never come back


def _bg_ids(path):
    """Every background-shell id this transcript has ever announced.

    Incremental, like session_ops: only the bytes appended since the last pass
    are scanned, so a 20 MB transcript costs nothing to follow. A transcript that
    shrank was replaced (a /clear starts a new session file), so the offset and
    the id list reset with it.
    """
    st = _BG_SEEN.setdefault(path, {"off": 0, "ids": []})
    try:
        size = os.path.getsize(path)
    except OSError:
        return st["ids"]
    if size < st["off"]:
        st["off"], st["ids"] = 0, []
    if size > st["off"]:
        try:
            with open(path, "rb") as f:
                f.seek(st["off"])
                chunk = f.read(size - st["off"]).decode("utf-8", "ignore")
        except OSError:
            return st["ids"]
        st["off"] = size
        for m in _BG_RE.finditer(chunk):
            tid = m.group(1)
            if tid not in st["ids"]:
                st["ids"].append(tid)
        if len(st["ids"]) > _BG_KEEP:
            del st["ids"][:-_BG_KEEP]
    return st["ids"]


async def _live_task_ids():
    """Task ids whose output file some process still holds open."""
    paths = {}
    for p in glob.glob(_BG_OUT):
        paths[os.path.basename(p)[:-len(".output")]] = p
    if not paths:
        return set()
    live = set()
    try:
        proc = await asyncio.create_subprocess_exec(
            "lsof", "-Fn", "--", *list(paths.values())[:256],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
    except Exception:
        return set()
    # lsof exits non-zero when SOME of the files have no readers, which is the
    # normal case here — the exit code says nothing, only the lines do.
    for line in (out or b"").decode(errors="ignore").splitlines():
        if not line.startswith("n"):
            continue
        base = os.path.basename(line[1:])
        if base.endswith(".output"):
            live.add(base[:-len(".output")])
    return live


async def _bg_sampler():
    while True:
        try:
            live = await _live_task_ids()
            counts = {}
            for rec in read_fleet_files().values():
                tp = rec.get("transcript")
                if not tp:
                    continue
                n = sum(1 for tid in _bg_ids(tp) if tid in live)
                if n:
                    counts[tp] = n
            _BG.clear()
            _BG.update(counts)
        except Exception as e:
            print(f"  [bg] sampler: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(10)


async def _load_sampler():
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "top", "-l", "2", "-n", "0", "-s", "1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=6)
            text = (out or b"").decode(errors="ignore")
            m = None
            for mm in re.finditer(
                    r"CPU usage:\s*([\d.]+)%\s*user,\s*([\d.]+)%\s*sys", text):
                m = mm                       # keep the LAST match (settled sample)
            if m:
                user, sysp = float(m.group(1)), float(m.group(2))
                _LOAD["cpu_user"] = user
                _LOAD["cpu_sys"] = sysp
                _LOAD["cpu_pct"] = round(user + sysp, 1)
                _LOAD["sampled"] = time.time()
        except Exception:
            pass

        try:
            proc = await asyncio.create_subprocess_exec(
                "ioreg", "-r", "-d", "1", "-c", "IOAccelerator",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=6)
            text = (out or b"").decode(errors="ignore")
            vals = [int(v) for v in
                    re.findall(r'"Device Utilization %"\s*=\s*(\d+)', text)]
            _LOAD["gpu_pct"] = max(vals) if vals else None
        except Exception:
            pass

        try:
            proc = await asyncio.create_subprocess_exec(
                "memory_pressure",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=6)
            text = (out or b"").decode(errors="ignore")
            m = re.search(r"free percentage:\s*(\d+)%", text)
            _LOAD["mem_free_pct"] = int(m.group(1)) if m else None
        except Exception:
            pass

        try:
            proc = await asyncio.create_subprocess_exec(
                "sysctl", "-n", "vm.swapusage",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=6)
            text = (out or b"").decode(errors="ignore")
            mu = re.search(r"used\s*=\s*([\d.]+)M", text)
            mt = re.search(r"total\s*=\s*([\d.]+)M", text)
            _LOAD["swap_used"] = int(float(mu.group(1)) * 1024 * 1024) if mu else None
            _LOAD["swap_total"] = int(float(mt.group(1)) * 1024 * 1024) if mt else None
        except Exception:
            pass

        # A real temperature without sudo. powermetrics' SMC sampler is
        # root-only, but the battery's gas gauge publishes its own sensors to the
        # IO registry in centi-°C: "Temperature" is the pack, "VirtualTemperature"
        # the gauge's compensated cell estimate. It is the one honest on-die-ish
        # number a plain user process can read, so it is what the page shows —
        # labelled for what it is rather than dressed up as a CPU die reading.
        try:
            proc = await asyncio.create_subprocess_exec(
                "ioreg", "-rn", "AppleSmartBattery",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=6)
            text = (out or b"").decode(errors="ignore")

            def _ioreg_int(key):
                m = re.search(r'"' + key + r'"\s*=\s*(-?\d+)', text)
                return int(m.group(1)) if m else None

            t = _ioreg_int("Temperature")
            _LOAD["temp_c"] = round(t / 100, 1) if t is not None else None
            tv = _ioreg_int("VirtualTemperature")
            _LOAD["temp_cell_c"] = round(tv / 100, 1) if tv is not None else None
            _LOAD["bat_cycles"] = _ioreg_int("CycleCount")
            nom, design = _ioreg_int("NominalChargeCapacity"), _ioreg_int("DesignCapacity")
            _LOAD["bat_health"] = (round(nom / design * 100)
                                   if nom and design else None)
        except Exception:
            pass

        try:
            proc = await asyncio.create_subprocess_exec(
                "pmset", "-g", "therm",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=6)
            text = (out or b"").decode(errors="ignore")
            m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", text)
            if m:
                n = int(m.group(1))
                _LOAD["cpu_limit"] = n
                _LOAD["thermal"] = "throttled" if n < 100 else "nominal"
            else:
                _LOAD["cpu_limit"] = None
                _LOAD["thermal"] = "nominal"   # only "Note:" lines, or command absent
        except Exception:
            pass

        await asyncio.sleep(10)


def _sysinfo_battery():
    out = {"percent": None, "state": None, "on_ac": False,
           "remaining": None, "present": False}
    try:
        proc = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                               text=True, timeout=3)
        text = proc.stdout or ""
        lines = text.splitlines()
        out["on_ac"] = bool(lines and "AC Power" in lines[0])
        if "InternalBattery" not in text:
            out["present"] = False
            out["on_ac"] = True
            return out
        out["present"] = True
        m = re.search(r"(\d+)%", text)
        if m:
            out["percent"] = int(m.group(1))
        m = re.search(r";\s*([a-zA-Z ]+?);", text)
        if m:
            word = m.group(1).strip().lower()
            if word == "finishing charge":
                out["state"] = "charging"
            elif word == "ac attached, not charging" or word == "ac attached; not charging":
                out["state"] = "ac"
            elif word in ("charging", "discharging", "charged"):
                out["state"] = word
            else:
                out["state"] = word
        m = re.search(r"(\d+:\d+) remaining", text)
        if m:
            out["remaining"] = m.group(1)
    except Exception:
        pass
    return out


def _sysinfo_disk():
    try:
        u = shutil.disk_usage("/")
        return {"total": u.total, "free": u.free, "used": u.used}
    except Exception:
        return {"total": None, "free": None, "used": None}


def _sysinfo_memory():
    total = None
    used = None
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"],
                                    capture_output=True, text=True,
                                    timeout=3).stdout.strip())
    except Exception:
        pass
    try:
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True,
                             timeout=3).stdout
        m = re.search(r"page size of (\d+) bytes", vm)
        pagesize = int(m.group(1)) if m else 4096
        def _pages(label):
            mm = re.search(re.escape(label) + r"\s*(\d+)\.", vm)
            return int(mm.group(1)) if mm else 0
        active = _pages("Pages active:")
        wired = _pages("Pages wired down:")
        compressor = _pages("Pages occupied by compressor:")
        used = (active + wired + compressor) * pagesize
    except Exception:
        pass
    return {"total": total, "used": used}


def _sysinfo_cpu():
    brand = None
    load = None
    cores = None
    try:
        brand = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                capture_output=True, text=True,
                                timeout=3).stdout.strip() or None
    except Exception:
        pass
    try:
        load = list(os.getloadavg())
    except Exception:
        pass
    try:
        cores = os.cpu_count()
    except Exception:
        pass
    return {"brand": brand, "load": load, "cores": cores}


def _sysinfo_uptime():
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                              capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"sec = (\d+)", out)
        if m:
            return int(time.time() - int(m.group(1)))
    except Exception:
        pass
    return None


def _sysinfo_sha():
    try:
        proc = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True, timeout=3)
        return proc.stdout.strip() or None
    except Exception:
        return None


def _sysinfo_agents():
    """Fleet-derived counts for the load page — panes needing input, actively
    working, or finished, plus how many phones are watching the live feed."""
    rows = _FLEET_CACHE
    return {
        "panes": len(rows),
        "sendable": sum(1 for r in rows if r.get("sendable")),
        "waiting": sum(1 for r in rows if r.get("prompt")),
        "running": sum(1 for r in rows if r.get("state") == "working"),
        "ended": sum(1 for r in rows if r.get("state") == "ended"),
        "ws_clients": len(_WS_FLEET),
        # POLL (0.45s) drives per-pane ws_pane loops, one per open chat, not a
        # single fleet-wide sampler — no one iteration wall time to report.
        "poll_ms": None,
        "iterm": CONN is not None,
    }


def _sysinfo_dep(provider):
    try:
        found = agent_bin(provider)
    except Exception:
        return None
    if found and os.path.isabs(found):
        return found
    return None


async def api_sysinfo(request):
    """Host diagnostics for the settings/diagnostics page."""
    if not authed(request):
        return web.json_response({"error": "locked"}, status=401)

    try:
        macos = platform.mac_ver()[0] or None
    except Exception:
        macos = None
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = None
    try:
        tailnet = ts_self_host()
    except Exception:
        tailnet = None
    try:
        origin = self_origin()
    except Exception:
        origin = None

    try:
        py_version = platform.python_version()
    except Exception:
        py_version = None

    try:
        tailscale_present = bool(shutil.which("tailscale") or
                                  os.path.exists(_TAILSCALE) or
                                  os.path.exists(
                                      "/Applications/Tailscale.app/Contents/MacOS/Tailscale"))
    except Exception:
        tailscale_present = False

    try:
        vapid_configured = os.path.exists(_VAPID_PRIV)
    except Exception:
        vapid_configured = False
    try:
        subscribers = len(_load_subs())
    except Exception:
        subscribers = 0

    try:
        peers = tailnet_macs()
    except Exception:
        peers = []

    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = None

    return web.json_response({
        "battery": _sysinfo_battery(),
        "disk": _sysinfo_disk(),
        "memory": _sysinfo_memory(),
        "cpu": _sysinfo_cpu(),
        "host": {
            "hostname": hostname,
            "tailnet": tailnet,
            "origin": origin,
            "macos": macos,
            "uptime": _sysinfo_uptime(),
        },
        "server": {
            "pid": os.getpid(),
            "started": _STARTED,
            "uptime": int(time.time() - _STARTED),
            "sha": _sysinfo_sha(),
            "bind": BIND,
            "port": PORT,
            "fleet_dir": FLEET_DIR,
            "iterm": CONN is not None,
            # who has a pane maximized right now, and for whom — the state
            # behind a "pane closed" that should not have been
            "grown": _GROWN[0][:8] if _GROWN else None,
            "watchers": {u[:8]: n for u, n in _WATCHERS.items()},
            "python": py_version,
            "panes": len(_FLEET_CACHE),
        },
        "deps": {
            "claude": _sysinfo_dep("claude"),
            "codex": _sysinfo_dep("codex"),
            "grok": _sysinfo_dep("grok"),
            "whisper": WHISPER_BIN or None,
            "ffmpeg": FFMPEG_BIN or None,
            "whisper_model": os.path.exists(WHISPER_MODEL),
            "tailscale": tailscale_present,
        },
        "push": {
            "configured": vapid_configured,
            "subscribers": subscribers,
        },
        "peers": peers,
        "load": {
            "cpu_pct": _LOAD.get("cpu_pct"),
            "cpu_user": _LOAD.get("cpu_user"),
            "cpu_sys": _LOAD.get("cpu_sys"),
            "gpu_pct": _LOAD.get("gpu_pct"),
            "mem_free_pct": _LOAD.get("mem_free_pct"),
            "swap_used": _LOAD.get("swap_used"),
            "swap_total": _LOAD.get("swap_total"),
            "thermal": _LOAD.get("thermal"),
            "temp_c": _LOAD.get("temp_c"),
            "temp_cell_c": _LOAD.get("temp_cell_c"),
            "bat_cycles": _LOAD.get("bat_cycles"),
            "bat_health": _LOAD.get("bat_health"),
            "cpu_limit": _LOAD.get("cpu_limit"),
            "load1": load1, "load5": load5, "load15": load15,
            "cores": os.cpu_count(),
            "sampled": _LOAD.get("sampled"),
        },
        "agents": _sysinfo_agents(),
    })


_PING_BAT = {"t": 0.0, "v": None}


def _ping_battery():
    """Charge level for the Devices list on every OTHER machine.

    /api/ping is polled by each peer that has the system page open, so the
    pmset shell-out behind it is cached: a charge level is slow-moving and a
    quarter-minute stale is invisible in a badge.
    """
    now = time.monotonic()
    if _PING_BAT["v"] is None or now - _PING_BAT["t"] > 15:
        b = _sysinfo_battery()
        _PING_BAT["v"] = {"percent": b.get("percent"), "state": b.get("state"),
                          "on_ac": bool(b.get("on_ac")),
                          "present": bool(b.get("present"))}
        _PING_BAT["t"] = now
    return _PING_BAT["v"]


async def api_ping(request):
    """Cross-origin reachability probe. Unauthenticated and CORS-open ON PURPOSE:
    the swapper on device A fetches deviceB/api/ping to light its status dot, a
    cross-origin GET. It reveals only that Dispatch is up, the tailnet hostname,
    this server's own public origin and its charge level — all already visible
    on the tailnet — and never reads a cookie, so opening it wide costs nothing.
    `url` lets the swapper adopt the peer's authoritative origin. It is the ONLY
    such route."""
    self_ = next((d for d in tailnet_macs() if d["self"]), {})
    return web.json_response(
        {"dispatch": True, "host": ts_self_host(),
         "url": self_origin(), "name": self_.get("name", ""),
         "battery": _ping_battery()},
        headers={"Access-Control-Allow-Origin": "*"})


# ── hot reload ───────────────────────────────────────────────────────────────
# A code change is deployed by pulling it and sending this process SIGHUP, NOT by
# an external relaunch: the server holds an iTerm2 API connection authorised by
# the ITERM2_COOKIE it was launched with, and only a process that inherits that
# cookie can reconnect without a GUI trust prompt. os.execv re-runs the script in
# the SAME process, so the cookie (and every other inherited env var) carries over
# and iTerm2 re-authorises silently. The listening socket is close-on-exec, so the
# fresh image rebinds PORT cleanly. Runtime state (sessions, vault, .token) lives
# on disk and is reloaded on boot, so nothing is lost across the swap.
PID_FILE = HERE / ".server.pid"
_SERVING = False        # True once our TCPSite owns PORT — guards reconnect re-entry


def _write_pidfile():
    try:
        PID_FILE.write_text(str(os.getpid()))
    except Exception as e:
        print(f"  [reload] pidfile write failed: {type(e).__name__}: {e}", flush=True)


def _install_hot_reload(runner):
    _reloading = False

    async def _do():
        nonlocal _reloading
        if _reloading:
            return
        _reloading = True
        print("  [reload] SIGHUP — draining socket, then re-exec", flush=True)
        try:
            # Bounded: an open websocket makes runner.cleanup() wait forever, which
            # used to leave the process alive, unbound and never re-exec'd — a
            # deploy that silently took the server down. Five seconds, then go.
            await asyncio.wait_for(runner.cleanup(), timeout=5)
        except Exception as e:
            print(f"  [reload] drain failed (continuing): {type(e).__name__}: {e}", flush=True)
        try:
            sys.stdout.flush(); sys.stderr.flush()
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable, str(HERE / "server.py")])

    def _on_hup():
        asyncio.ensure_future(_do())

    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, _on_hup)
        print("  [reload] SIGHUP hot-reload armed", flush=True)
    except (NotImplementedError, RuntimeError) as e:
        print(f"  [reload] hot-reload unavailable: {type(e).__name__}: {e}", flush=True)


async def main(connection):
    global CONN, APP
    try:
        import icon as icon_art
        icon_art.write_all(HERE / "static" / "icons")
    except Exception as e:
        print(f"  [icons] not regenerated: {type(e).__name__}: {e}", flush=True)
    CONN = connection
    APP = await iterm2.async_get_app(connection)

    # aiohttp caps a request body at 1 MB by default and answers anything larger
    # with its own bare 413 — which is what every phone photo hit, long before
    # api_upload's own limit could have an opinion. Raise the ceiling to just
    # past the upload limit so the handler is the thing that enforces it and can
    # say what happened; api_upload streams to disk, so a big body is never held
    # in memory whole.
    app = web.Application(client_max_size=_UPLOAD_MAX + 4 * 1024 * 1024)
    app["TOKEN"] = TOKEN
    app.router.add_get("/", index)
    app.router.add_get("/api/fleet", api_fleet)
    app.router.add_get("/api/summary", api_summary)
    app.router.add_post("/api/key", api_key)
    app.router.add_post("/api/prompt", api_prompt)
    app.router.add_post("/api/select", api_select)
    app.router.add_post("/api/send", api_send)
    app.router.add_post("/api/whisper", api_whisper)
    app.router.add_post("/api/upload", api_upload)
    app.router.add_post("/api/submit", api_submit)
    app.router.add_post("/api/spawn", api_spawn)
    app.router.add_get("/api/browse", api_browse)
    app.router.add_post("/api/kill", api_kill)
    app.router.add_post("/api/reap", api_reap)
    app.router.add_post("/api/effort", api_effort)
    app.router.add_post("/api/mode", api_mode)
    app.router.add_post("/api/model", api_model)
    app.router.add_post("/api/cmd", api_cmd)
    app.router.add_get("/api/commands", api_commands)
    app.router.add_get("/api/codex/models", api_codex_models)
    app.router.add_get("/api/grok/models", api_grok_models)
    app.router.add_get("/api/usage", api_usage)
    app.router.add_get("/api/devices", api_devices)
    app.router.add_post("/api/wake", api_wake)
    app.router.add_post("/api/sleep", api_sleep)
    app.router.add_get("/api/sysinfo", api_sysinfo)
    app.router.add_get("/api/ping", api_ping)
    app.router.add_get("/api/history", api_history)
    app.router.add_post("/api/resume", api_resume)
    app.router.add_get("/api/vapid", api_vapid)
    app.router.add_post("/api/push/subscribe", api_push_subscribe)
    app.router.add_post("/api/push/unsubscribe", api_push_unsubscribe)
    app.router.add_post("/api/push/test", api_push_test)
    app.router.add_get("/sw.js", serve_sw)
    # agent auth broker — phone side (passkey-gated)
    app.router.add_get("/api/integrations", api_integrations)
    app.router.add_post("/api/integrations", api_integrations_add)
    app.router.add_post("/api/integrations/device/start", api_device_start)
    app.router.add_post("/api/integrations/device/poll", api_device_poll)
    app.router.add_post("/api/integrations/{id}/scope", api_integration_scope)
    app.router.add_post("/api/integrations/{id}/delete", api_integration_delete)
    app.router.add_get("/api/requests", api_requests)
    app.router.add_post("/api/requests/{id}/approve", api_request_approve)
    app.router.add_post("/api/requests/{id}/deny", api_request_deny)
    app.router.add_get("/api/grants", api_grants)
    app.router.add_post("/api/grants/{id}/revoke", api_grant_revoke)
    # agent auth broker — agent side (loopback-only, pane-token gated)
    app.router.add_post("/agent/request", agent_request)
    app.router.add_post("/agent/fetch", agent_fetch)
    app.router.add_post("/agent/list", agent_list)
    # passkey enrolment and unlock — the only endpoints a locked session may use
    app.router.add_get("/manifest.webmanifest", manifest)
    app.router.add_get("/icons/{name}", icon)
    app.router.add_post("/api/clientlog", client_log)
    app.router.add_post("/auth/bootstrap", auth.bootstrap)
    app.router.add_get("/auth/whoami", auth.whoami)
    app.router.add_post("/auth/register/begin", auth.register_begin)
    app.router.add_post("/auth/register/complete", auth.register_complete)
    app.router.add_post("/auth/login/begin", auth.login_begin)
    app.router.add_post("/auth/login/complete", auth.login_complete)
    app.router.add_post("/auth/logout", auth.logout)
    app.router.add_get("/ws/fleet", ws_fleet)
    app.router.add_get("/ws/pane/{uuid}", ws_pane)

    runner = web.AppRunner(app)
    await runner.setup()
    # iTerm2's client re-invokes main() whenever its API socket reconnects. A
    # second entry finds PORT already held by our first, live runner — that's
    # expected, not a failure, so swallow EADDRINUSE and let the original keep
    # serving instead of crashing the reconnect with a traceback.
    global _SERVING
    try:
        await web.TCPSite(runner, BIND, PORT).start()
    except OSError as e:
        if e.errno == errno.EADDRINUSE and _SERVING:
            print("  [reload] iTerm2 reconnect re-entered main(); already serving, "
                  "keeping the live server", flush=True)
            return
        raise
    _SERVING = True

    _write_pidfile()
    _install_hot_reload(runner)                 # `kill -HUP` re-execs in place (deploy)

    asyncio.create_task(_notify_watcher())     # push when sessions finish / need input
    asyncio.create_task(_grow_janitor())       # never leave a pane maximized with nobody watching
    asyncio.create_task(_rebuild_history())    # warm the history cache so first open is instant
    asyncio.create_task(_load_sampler())       # background CPU/GPU/thermal snapshot for /api/sysinfo
    asyncio.create_task(_bg_sampler())         # which chats still have a background shell alive

    async def _normalize_once():
        await asyncio.sleep(2)                 # let APP settle after (re-)connect
        await normalize_all()                  # widen existing narrow panes post-deploy
    asyncio.create_task(_normalize_once())

    # Where the phone should actually point: the tailnet name, over TLS, served
    # by `tailscale serve`. Falls back to the raw bind address if the tunnel is
    # not up yet — and says so, loudly, because that path has no passkey.
    # Use the full serve origin — port included — so a device published on a
    # non-443 port (e.g. :8443 when its root is taken) hands out a URL that works.
    serve_origin = ts_serve_origin()
    base = serve_origin or f"http://{BIND}:{PORT}"
    phone_url = f"{base}/?t={TOKEN}"
    print(f"\n  CC Dispatch — bound to {BIND}:{PORT}\n")
    if BIND not in ("127.0.0.1", "::1", "localhost"):
        print("  !! WARNING: not bound to loopback. Anyone who can reach this\n"
              "     address can attempt the bootstrap token. Prefer loopback +\n"
              "     `tailscale serve`.\n")
    if not serve_origin:
        print("  !! `tailscale serve` is not running — no TLS, and passkeys\n"
              "     cannot be registered over a bare IP. Start it with:\n"
              f"       tailscale serve --bg {PORT}\n")
    # Scan to launch: the token rides in the QR, so the phone opens straight into
    # the fleet with no typing. Falls back to the bare URL if qrcode isn't present.
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(phone_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as e:
        print(f"  (no QR — {type(e).__name__}: {e})")
    print(f"  phone :  {phone_url}")
    print(f"  local :  http://127.0.0.1:{PORT}/?t={TOKEN}")
    print(f"  passkeys registered: {len(auth.load_creds())}"
          f"   audit: {auth.AUDIT_FILE}\n")
    fleet, _ = await build_fleet()
    tally = ", ".join(f"{sum(1 for f in fleet if f['provider'] == p)} {p}"
                      for p in PROVIDERS if any(f["provider"] == p for f in fleet))
    print(f"  {len(fleet)} agent panes visible ({tally or 'none'}), "
          f"{sum(1 for f in fleet if f['sendable'])} sendable\n", flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    iterm2.run_until_complete(main)
