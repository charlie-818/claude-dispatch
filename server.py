#!/usr/bin/env python3
"""CC Dispatch — phone control for a fleet of live Claude Code panes.

Reads and drives EXISTING iTerm2 sessions via the iTerm2 Python API. Nothing is
restarted. The one exception to "no session is created" is /api/spawn, which opens
a new Claude pane in a throwaway scratch dir on explicit request from the UI.

Safety model
------------
* Writes only ever happen in response to an authenticated request from the UI.
* SEND_ALLOW gates every write: a pane must be running Claude to receive keys,
  so scratch shells and unrelated panes are unreachable by construction.
* Killing this process leaves the fleet exactly as it was.

Run:  .venv/bin/python server.py
"""
import asyncio, json, os, re, secrets, sys, time, glob, pathlib, subprocess
from aiohttp import web, WSMsgType
import iterm2
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
STALE = 90                       # fleet json older than this is dropped

# A pane may receive keystrokes if its foreground job is Claude itself, OR if
# statusline.sh has recently written a fleet file for it. The second clause
# matters: while Claude runs a Bash tool the foreground job is that child
# process (bash/git/npm...), and gating on jobName alone would refuse Esc and
# Ctrl-C at precisely the moment you need them.
SEND_ALLOW = {"claude", "node", "caffeinate"}

# Panes confirmed to be Claude at any point. Identity does not expire: a pane
# sitting at a permission prompt stops re-rendering its statusline, so its
# fleet file ages out of the STALE window — and that is precisely when you need
# to answer it. Gating on freshness locked out exactly the wrong case.
KNOWN_CLAUDE = set()

# Markers unique to Claude's TUI chrome, used as a last-resort identity check
# when jobName is a child process and no fleet file is present.
CLAUDE_MARKERS = ("shift+tab to cycle", "for shortcuts", "bypass permissions on",
                  "auto mode on", "plan mode on", "accept edits on",
                  "manual mode on", "esc to interrupt")


def looks_like_claude(text):
    low = (text or "").lower()
    return any(m in low for m in CLAUDE_MARKERS)


def is_claude_pane(uuid, job, text=None):
    u = (uuid or "").upper()
    if job in SEND_ALLOW or u in KNOWN_CLAUDE:
        return True
    if u in read_fleet_files() or looks_like_claude(text):
        KNOWN_CLAUDE.add(u)
        return True
    return False

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


# ── iTerm helpers ──────────────────────────────────────────────────────────
async def all_sessions():
    """Every live pane, keyed by uppercase session UUID."""
    await APP.async_refresh()
    out = {}
    for w in APP.terminal_windows:
        for t in w.tabs:
            for s in t.sessions:
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
            n = sum(1 for s in t.sessions if s.session_id.upper() in KNOWN_CLAUDE)
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
    import json, os
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


async def pane_history(session, max_lines=600):
    """Scrollback above the visible screen, so the phone can read the whole chat.

    Absolute line numbers run from `overflow` (oldest line iTerm still holds)
    upward; anything below that has already been discarded.
    """
    try:
        info = await session.async_get_line_info()
        start = info.overflow
        end = info.overflow + info.scrollback_buffer_height
        if end <= start:
            return []
        first = max(start, end - max_lines)
        lines = await session.async_get_contents(first, end - first)
        return [l.string.replace("\x00", " ").rstrip() for l in lines]
    except Exception as e:
        print(f"  [history] failed: {type(e).__name__}: {e}", flush=True)
        return []


async def pane_cols(session):
    try:
        return int(session.grid_size.width)
    except Exception:
        return 80


async def pane_text(session):
    c = await session.async_get_screen_contents()
    # iTerm returns NUL for every unwritten cell, not space. NULs are stripped
    # by innerHTML, which collapses the layout — translate them back to spaces.
    return "\n".join(c.line(i).string.replace("\x00", " ").rstrip()
                     for i in range(c.number_of_lines)).rstrip()


OPTION_RE = re.compile(r"^\s*[❯>]?\s*(\d)\.\s+(\S.*?)\s*$")


def detect_input(text):
    """The text sitting in Claude's ❯ input box — a greyed ghost suggestion
    (rendered after a NON-breaking space) or already-typed/queued text. We strip
    the box from the phone's pane view, so surface this so it still shows in the
    mobile composer. Returns {"text","ghost"} or None."""
    for l in reversed(text.splitlines()):
        i = l.find("❯")
        if i == -1:
            continue
        rest = l[i + 1:]
        ghost = rest[:1] == "\xa0"                 # ❯\xa0… = suggestion, ❯ … = typed
        s = rest.replace("\xa0", " ").strip(" │╎")
        if not s:
            return None
        return {"text": s[:200], "ghost": ghost}
    return None


def detect_prompt(text):
    """Find a numbered choice Claude is waiting on (permission, plan approval).

    Returns {"question": str, "options": [{"key","label","selected"}]} or None.
    Only the LAST run of consecutive numbered lines counts — earlier ones are
    scrollback from prompts already answered.
    """
    lines = text.splitlines()
    runs, cur = [], []
    label_col = 0                      # column where the current run's labels start

    def opt(i, l, m):
        return [i, m.group(1), m.group(2), "❯" in l]

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
            cur.append(opt(i, l, m))
            continue
        if not cur:
            continue
        s = l.strip()
        # A narrow pane (a split can be 16 columns wide) hard-wraps every option
        # onto continuation lines indented to the label column. Those belong to
        # the option above, not to the end of the run — without this, no prompt
        # on a narrow pane is ever detected.
        if s and indent >= label_col and not set(s) <= set("─━│ ⎿"):
            cur[-1][2] = (cur[-1][2] + " " + s)[:200]
            continue
        if not s:
            continue                   # blank gutter between options is fine
        close()
    close()
    runs = [r for r in runs if len(r) >= 2]
    if not runs:
        return None
    run = runs[-1]

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
        if OPTION_RE.match(lines[j]) or set(cand) <= set("─━│ ⎿"):
            break
        parts.append(cand)
    q = " ".join(reversed(parts))
    return {
        "question": q[:160],
        "options": [{"key": k, "label": lbl[:70], "selected": sel}
                    for _, k, lbl, sel in run][:9],
    }


_EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# Harness-injected user lines that aren't something a human typed — excluded from
# the prompt count, mirroring cc-dashboard's _NOISE filter.
_PROMPT_NOISE = ("Caveat:", "<command-name>", "<command-message>", "<local-command",
                 "[Request interrupted", "system-reminder", "<user-prompt-submit")
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
                if txt.strip() and not any(s in txt for s in _PROMPT_NOISE):
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


def read_fleet_files():
    """Status dumps written by ~/.claude/statusline.sh + cc-active.sh.

    We only read these; cc-dashboard.py owns them and prunes its own staleness.
    """
    now, out = time.time(), {}
    for p in glob.glob(os.path.join(FLEET_DIR, "*.json")):
        try:
            if now - os.path.getmtime(p) > STALE:
                continue
            d = json.load(open(p))
        except Exception:
            continue
        pane = d.get("iterm_pane") or ""
        uuid = pane.split(":")[-1].upper() if ":" in pane else ""
        if not uuid:
            continue
        key = pathlib.Path(p).stem
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
        cw = d.get("context_window") or {}
        cst = d.get("cost") or {}
        tp = d.get("transcript_path") or ""
        out[uuid] = {
            "sid": key,
            "transcript": tp,
            "state": state,
            # When this pane entered its current state, straight from the .state
            # file the working/idle hooks touch — the SAME clock cc-dashboard and
            # the iTerm tab colour use, so the phone timer matches them and, being
            # on disk, survives a server restart.
            "state_since": state_mt,
            "cwd": (d.get("workspace") or {}).get("current_dir", ""),
            "model": (d.get("model") or {}).get("display_name", ""),
            "cost": round(cst.get("total_cost_usd", 0) or 0, 2),
            "ctx": cw.get("used_percentage"),
            "limits": d.get("rate_limits") or {},
            "effort": (d.get("effort") or {}).get("level"),
            "mtime": os.path.getmtime(p),
            # richer per-agent metrics, ported from cc-dashboard's fleet row
            "tokens": (cw.get("total_input_tokens") or 0)
                    + (cw.get("total_output_tokens") or 0),
            "lines_add": cst.get("total_lines_added") or 0,
            "lines_del": cst.get("total_lines_removed") or 0,
            "dur_ms": cst.get("total_duration_ms") or 0,
            "age": int(now - os.path.getmtime(p)),
        }
        fcount, pcount = session_ops(tp) if tp else (0, 0)
        out[uuid]["files"] = fcount
        out[uuid]["prompts"] = pcount
        out[uuid]["action"] = (_OPS.get(tp) or {}).get("action") if tp else None
    return out


def fleet_limits(files):
    """Account-wide 5h/7d usage. Each pane only refreshes its own copy on an
    API call, so take the reading from the newest rate-limit window."""
    best, best_key = {}, (-1, -1)
    for f in files.values():
        fh = (f.get("limits") or {}).get("five_hour")
        if isinstance(fh, dict):
            k = (fh.get("resets_at", 0), fh.get("used_percentage", -1))
            if k > best_key:
                best_key, best = k, f.get("limits")
    return best


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


async def build_fleet():
    sessions = await all_sessions()
    files = read_fleet_files()
    rows = []
    for uuid, s in sessions.items():
        f = files.get(uuid)
        try:
            job = await s.async_get_variable("jobName") or ""
            cwd = await s.async_get_variable("path") or ""
        except Exception:
            job, cwd = "", ""
        txt = await pane_text(s)
        if not is_claude_pane(uuid, job, txt):
            continue                      # hide scratch shells entirely
        lines = [l for l in txt.splitlines() if l.strip()]
        mode = detect_mode(txt)
        prompt = detect_prompt(txt)
        # live working dir: the transcript's current cwd (follows `cd`s), then the
        # statusline's launch-pinned dir, then the iTerm pane path — same order as ccdash
        live_cwd = latest_cwd((f or {}).get("transcript")) or (f or {}).get("cwd") or cwd
        rows.append({
            "uuid": uuid,
            "job": job,
            "cwd": live_cwd,
            "name": os.path.basename(live_cwd.rstrip("/")) or "?",
            "state": (f or {}).get("state", "idle"),
            "model": (f or {}).get("model", ""),
            "ctx": (f or {}).get("ctx"),
            "cost": (f or {}).get("cost"),
            "effort": (f or {}).get("effort"),
            "mode": mode,
            "prompt": prompt,
            "sendable": is_claude_pane(uuid, job, txt),
            "tokens": (f or {}).get("tokens"),
            "lines_add": (f or {}).get("lines_add"),
            "lines_del": (f or {}).get("lines_del"),
            "files": (f or {}).get("files"),
            "prompts": (f or {}).get("prompts"),
            "age": (f or {}).get("age"),
            "dur_ms": (f or {}).get("dur_ms"),
            "work_since": (f or {}).get("state_since"),
            "action": (f or {}).get("action"),   # newest tool call, from transcript
            # Enough lines that the current `⏺ Tool(args)` action line is in the
            # window — it sits several lines above the bottom, behind its `⎿`
            # result, the spinner and the prompt box. The bubble distiller scans
            # this backwards for the newest tool call to show what Claude's doing.
            "tail": lines[-14:],
        })
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
# The summary of a run barely changes and its stop condition even less, so we do
# NOT re-summarise on a timer. Instead we compute ONCE per user prompt — keyed on
# the transcript's prompt count — and reuse it for the whole run. Sending a new
# prompt bumps the count and is what triggers the next (single) summarisation,
# fired proactively the moment the prompt goes out. Results persist to disk so a
# restart reuses them instead of paying for a fresh call.
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
        role = (o.get("message") or {}).get("role") or o.get("type")
        content = (o.get("message") or {}).get("content")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text")
        if not isinstance(content, str) or not content.strip():
            continue
        if any(n in content for n in _PROMPT_NOISE):
            continue
        msgs.append(f"{role}: {content.strip()}")
    if not msgs:
        return ""
    first = msgs[0]
    tail = "\n".join(msgs[1:])
    if len(tail) > max_chars:
        tail = "…" + tail[-max_chars:]
    return (first + "\n" + tail)[:max_chars + 2000]


async def _claude_summary(digest, running):
    """Ask `claude -p` for the two lines. Returns (summary, success|None)."""
    ask = (
        "You are labeling a Claude Code coding session for a phone status bar. "
        "Below is a transcript digest (first prompt, then recent messages). "
        "Reply with EXACTLY two lines and nothing else:\n"
        "SUMMARY: <one sentence, <=110 chars, what this session has been doing overall>\n"
        "SUCCESS: <" + (
            "one sentence, <=110 chars, the concrete condition that will make the "
            "current task stop/finish>" if running else "the word NONE") + "\n\n"
        "Transcript digest:\n" + digest)
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", ask,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
    except Exception as e:
        print(f"  [summary] claude -p failed: {type(e).__name__}: {e}", flush=True)
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


async def _ensure_summary(uuid, path, pcount, running):
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
        summary, success = await _claude_summary(digest, running)
        entry = {"prompts": pcount, "summary": summary, "success": success,
                 "at": time.time()}
        _summaries[uuid] = entry
        await asyncio.to_thread(_save_summaries)
        print(f"  [summary] {uuid[:8]} summarised at prompt #{pcount}", flush=True)
        return entry


async def _summarise_after_send(uuid):
    """Fire-and-forget: right after a prompt is sent, wait for Claude to write it
    into the transcript, then compute+store this run's summary so it's ready
    before the phone ever asks."""
    await asyncio.sleep(2.5)
    try:
        f = read_fleet_files().get(uuid) or {}
        path = f.get("transcript")
        if not path or not os.path.exists(path):
            return
        _, pcount = session_ops(path)
        await _ensure_summary(uuid, path, pcount, f.get("state") == "working")
    except Exception as e:
        print(f"  [summary] after-send failed: {type(e).__name__}: {e}", flush=True)


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
    _, pcount = session_ops(path)
    entry = await _ensure_summary(uuid, path, pcount, running) or {}
    return web.json_response({"summary": entry.get("summary"),
                              "success": entry.get("success"),
                              "running": running, "prompts": pcount})


# ── auth ───────────────────────────────────────────────────────────────────
# The token is a BOOTSTRAP credential only: it is accepted once, at "/", and
# immediately exchanged for an HttpOnly session cookie. No API or socket ever
# looks at it, so it cannot be replayed from a URL, a screenshot or a log.
def authed(request):
    return auth.unlocked(request) is not None


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
        return await handler(request)
    return wrapped


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
            return await handler(request)
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
        "background_color": "#14171b", "theme_color": "#22262d",
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
    if not is_claude_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)
    await s.async_send_text(KEYS[k])
    return web.json_response({"ok": True, "sent": k})


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
    if not is_claude_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)
    # literal, byte-exact: $ ` " ' and newlines all survive (probe_keys.py test 1)
    await s.async_send_text(text)
    if submit:
        await asyncio.sleep(0.15)
        await s.async_send_text("\r")
        asyncio.create_task(_summarise_after_send(uuid))   # refresh the brief
    return web.json_response({"ok": True, "chars": len(text)})


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


@guard
async def api_upload(request):
    uuid = (request.headers.get("X-Uuid") or "").upper()
    fname = _safe_name(request.headers.get("X-Filename") or "file")
    raw = await request.read()
    if not raw:
        return web.json_response({"error": "no file"}, status=400)
    if len(raw) > _UPLOAD_MAX:
        return web.json_response({"error": "file too large"}, status=413)
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
    try:
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, fname)
        # never clobber: add -1, -2 … if the name exists
        stem, ext = os.path.splitext(fname)
        n = 1
        while os.path.exists(path):
            path = os.path.join(dest_dir, f"{stem}-{n}{ext}"); n += 1
        await asyncio.to_thread(lambda: open(path, "wb").write(raw))
    except Exception as e:
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)
    auth.audit(request, "upload", {"path": path, "bytes": len(raw)})
    print(f"  [upload] {len(raw)}B → {path}", flush=True)
    return web.json_response({"ok": True, "path": path, "name": os.path.basename(path)})


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
    nothing), so the caller retypes the suggestion as a real prompt instead.
    """
    for line in reversed(text.splitlines()):
        st = line.strip()
        if not st.startswith(_PROMPT_CARET):
            continue
        rest = st[len(_PROMPT_CARET):]
        if rest.startswith("\xa0"):
            return "ghost", rest[1:].strip()
        if rest.strip():
            return "typed", rest.strip()
        return "empty", ""
    return "empty", ""


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
    if not is_claude_pane(uuid, job, txt):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)
    kind, sug = _input_suggestion(txt)
    if kind == "ghost" and sug:
        await s.async_send_text(sug)          # retype the suggestion as real input
        await asyncio.sleep(0.15)
    await s.async_send_text("\r")             # submit (typed text, or the retype)
    asyncio.create_task(_summarise_after_send(uuid))       # refresh the brief
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


@writes("spawn")
async def api_spawn(request):
    """Open a brand-new Claude pane in the iTerm window and hand back its UUID.

    It starts in a throwaway scratch dir so nothing real is touched until you tell
    it where to work. The scratch dir is pre-trusted in ~/.claude.json so Claude's
    first-run "trust the files in this folder?" prompt never fires. We add the UUID
    to KNOWN_CLAUDE up front so it lands in the fleet the instant it opens, before
    its jobName has even settled to `node`.
    """
    import tempfile, shlex
    await APP.async_refresh()
    body = request.get("_body") or {}
    # Chosen dir from the picker: cd straight there. Absent → throwaway scratch, so
    # nothing real is touched until the owner picks a folder. Either way the launch
    # dir is trusted up front so Claude's first-run trust prompt never fires.
    chosen = (body.get("dir") or "").strip()
    if chosen:
        chosen = os.path.abspath(os.path.expanduser(chosen))
        if not os.path.isdir(chosen):
            return web.json_response({"error": "not a directory"}, status=400)
        workdir = chosen
    else:
        workdir = tempfile.mkdtemp(prefix="cc-scratch-")
    scratch = workdir
    is_scratch = not chosen
    trust_dir(scratch)                     # skip Claude's first-run trust prompt
    # Grow the fleet's own tab into a grid instead of opening a new tab. Panes are
    # placed row-major (see GRID_MAX_COLS) so the split lands in an aligned column
    # or row rather than as a random narrow sliver.
    try:
        tab = fleet_tab(APP)
        if tab is None:
            return web.json_response(
                {"error": "no iTerm window open to spawn into"}, status=409)
        src, vertical = pick_grid_split(tab)
        sess = await src.async_split_pane(vertical=vertical, before=False)
    except Exception as e:
        return web.json_response(
            {"error": f"could not open pane: {type(e).__name__}: {e}"}, status=500)
    uuid = sess.session_id.upper()
    KNOWN_CLAUDE.add(uuid)

    # Hand the pane a capability handle for the auth broker, plus any credentials
    # the owner chose to pre-inject. All of it goes through a 0600 file the shell
    # sources and deletes — the raw values are NEVER keystroked, so they can't land
    # in the pane's scrollback or shell history. DISPATCH_AGENT_TOKEN + DISPATCH_URL
    # are capability handles (not secrets); pre-injected service tokens are secrets.
    tok = secrets.token_urlsafe(24)
    PANE_TOKENS[tok] = uuid
    lines = [f'export PATH={shlex.quote(str(HERE))}:"$PATH"',
             f'export DISPATCH_URL="http://127.0.0.1:{PORT}"',
             f'export DISPATCH_AGENT_TOKEN={shlex.quote(tok)}']
    for cid in (body.get("integrations") or []):
        cred = vault.get_cred(cid)
        if not cred:
            continue
        lines.append(f'export {cred["env_var"]}={shlex.quote(cred["secret"])}')
        vault.add_grant(uuid, cid, scopes=cred.get("scopes") or [])   # visible + revocable
        auth.audit(request, "integ.release",
                   {"uuid": uuid, "cred": cid, "last4": cred["last4"], "via": "spawn"})
    envfile = os.path.join(scratch, ".cc-inject.env")
    fd = os.open(envfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, ("\n".join(lines) + "\n").encode())
    os.close(fd)
    # Teach the agent the protocol so it knows it can ask for what it lacks. Only
    # in a scratch dir — never drop a CLAUDE.md into a real repo the owner picked.
    if is_scratch:
        try:
            (pathlib.Path(scratch) / "CLAUDE.md").write_text(_AGENT_NOTE)
        except Exception:
            pass

    q = shlex.quote(scratch)
    await sess.async_send_text(
        f"cd {q} && set -a && . ./.cc-inject.env && rm -f ./.cc-inject.env && set +a"
        f" && clear && claude\n")
    # Fallback in case the pre-trust key got clobbered — auto-accept the trust
    # dialog if it still shows. Fire-and-forget so the spawn returns immediately.
    asyncio.create_task(_auto_trust(sess))
    print(f"  [spawn] new pane {uuid} in {scratch}", flush=True)
    return web.json_response({"uuid": uuid, "dir": scratch})


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


@guard
async def api_browse(request):
    """List directories under `path` so the phone can click through the filesystem
    and pick where a new pane starts. No path → the roots (home + volumes). Read
    only; never returns files, only sub-directories."""
    path = request.query.get("path", "")
    if not path:
        return web.json_response({"path": "", "parent": None,
                                  "roots": _browse_roots(), "dirs": []})
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return web.json_response({"error": "not a directory"}, status=404)
    dirs = []
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            if name.startswith("."):
                continue                       # hide dotfiles/dirs
            full = os.path.join(path, name)
            try:
                if os.path.isdir(full):
                    dirs.append({"name": name, "path": full})
            except OSError:
                continue
    except OSError as e:
        return web.json_response({"error": str(e)}, status=403)
    parent = os.path.dirname(path.rstrip("/"))
    if parent == path or not parent:
        parent = None
    return web.json_response({"path": path, "parent": parent,
                              "roots": _browse_roots(), "dirs": dirs})


@writes("kill")
async def api_kill(request):
    """End a session and close its iTerm pane — the drag-to-trash gesture.

    Ctrl-C first so Claude tears down its own child processes, then /exit so it
    saves the transcript the way a normal quit would, then close the pane. The
    close is what removes the split/tab from the Mac; without it the shell just
    returns to a prompt and the pane lingers.
    """
    d = await request.json()
    uuid = (d.get("uuid") or "").upper()
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    try:
        await s.async_send_text("\x03")          # interrupt whatever is running
        await asyncio.sleep(0.2)
        await s.async_send_text("/exit\r")       # let Claude close its session
        await asyncio.sleep(0.6)
    except Exception as e:
        print(f"  [kill] {uuid} graceful stop failed: {type(e).__name__}: {e}",
              flush=True)
    try:
        await s.async_close(force=True)
    except Exception as e:
        return web.json_response(
            {"error": f"could not close pane: {type(e).__name__}: {e}"}, status=500)
    KNOWN_CLAUDE.discard(uuid)
    print(f"  [kill] closed pane {uuid}", flush=True)
    return web.json_response({"ok": True, "uuid": uuid})


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


@writes("mode")
async def api_mode(request):
    """Shift-Tab until the requested mode is showing.

    The cycle order isn't assumed — we re-read the status line after each press
    and stop when it matches, so this stays correct if Claude reorders modes.
    """
    body = await request.json()
    uuid, want = body.get("uuid", "").upper(), body.get("mode")
    if want not in MODES:
        return web.json_response({"error": f"bad mode {want!r}"}, status=400)
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    if not is_claude_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)

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
    if level not in EFFORTS:
        return web.json_response({"error": f"bad level {level!r}"}, status=400)
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    if not is_claude_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)
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
    if name not in COMMANDS:
        return web.json_response({"error": f"command {name!r} not allowed"}, status=400)
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    if not is_claude_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)
    await send_slash(s, COMMANDS[name][0])
    return web.json_response({"ok": True, "cmd": COMMANDS[name][0]})


@guard
async def api_commands(request):
    return web.json_response(
        {"commands": [{"id": k, "cmd": v[0], "desc": v[1]}
                      for k, v in COMMANDS.items()]})


@writes("model")
async def api_model(request):
    """Fire /model <name>.

    WARNING: /model is NOT session-local. Claude echoes "saved as your default
    for new sessions" — it rewrites ~/.claude/settings.json, so this repoints
    every future session too. The UI requires a second confirming tap.
    """
    body = await request.json()
    uuid, name = body.get("uuid", "").upper(), body.get("model")
    if name not in MODELS:
        return web.json_response({"error": f"bad model {name!r}"}, status=400)
    s = (await all_sessions()).get(uuid)
    if not s:
        return web.json_response({"error": "no such pane"}, status=404)
    job = await s.async_get_variable("jobName") or ""
    if not is_claude_pane(uuid, job, await pane_text(s)):
        return web.json_response(
            {"error": f"pane is running {job!r} and shows no Claude UI — refusing"},
            status=403)
    await send_slash(s, f"/model {MODELS[name]}")
    return web.json_response({"ok": True, "model": name,
                              "note": "also changed the global default"})


# ── usage / CC Dash data ───────────────────────────────────────────────────
# Reuses ~/.claude/cc_history.py — the same module cc-dashboard.py reads, so the
# numbers here and in the TUI come from one source and cannot drift apart.
sys.path.insert(0, os.path.expanduser("~/.claude"))
_usage_cache = {"at": 0, "data": None}
USAGE_TTL = 120


def _compute_usage():
    # Only today. The dashboard is a "what is happening right now" screen — the
    # 30-day chart, per-project spend and all-time totals live in the TUI.
    import cc_history as HIST
    agg = HIST.build()          # build() already returns the aggregate
    tokens = HIST.series(agg, "tokens")
    cost = HIST.series(agg, "cost")
    today = time.strftime("%Y-%m-%d")
    tot = agg.get("tot") or {}
    # last 30 days of cost/tokens for the phone spend chart (oldest → newest, gaps
    # filled with 0 so the bar chart has one column per calendar day). Same series
    # the TUI's 30-day graph reads, so the shapes line up.
    from datetime import date as _date, timedelta as _td
    _t = _date.today()
    days = [( _t - _td(days=i)).isoformat() for i in range(29, -1, -1)]
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
    }


@guard
async def api_usage(request):
    now = time.time()
    if _usage_cache["data"] and now - _usage_cache["at"] < USAGE_TTL:
        data = _usage_cache["data"]
    else:
        try:
            data = await asyncio.to_thread(_compute_usage)
            _usage_cache.update(at=now, data=data)
        except Exception as e:
            print(f"  [usage] failed: {type(e).__name__}: {e}", flush=True)
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    rows, limits = await build_fleet()
    return web.json_response({
        **data,
        "limits": limits,
        "fleet": {"panes": len(rows),
                  "working": sum(1 for r in rows if r["state"] == "working"),
                  "live_cost": round(sum(r.get("cost") or 0 for r in rows), 2)},
        "age": int(now - _usage_cache["at"]),
        # Absolute epoch the data was computed at, so the client can tick the
        # freshness label off its own clock (same pattern as the fleet timers).
        "computed_at": _usage_cache["at"],
    })


# ── history: every agent that has come through the fleet ────────────────────
# Reads the top-level session transcripts (projects/<proj>/<session>.jsonl —
# subagent/workflow files live in subdirs and are skipped) and lists them newest
# first: title (first prompt), project, model, when, prompt count, tokens, cost.
# Tapping one resumes it via `claude --resume <session_id>` (see api_resume).
_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
_hist_cache = {}      # path -> {"k":[mtime,size], "e":entry}
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,}$")


def _scan_history_file(path):
    try:
        lines = pathlib.Path(path).read_text(errors="replace").splitlines()
    except Exception:
        return None
    import cc_history as HIST
    from datetime import datetime
    cwd = None; model = None; first = last = None; prompts = 0
    itok = otok = cctok = crtok = 0; seen = set(); title = ""
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
        # first genuine user prompt → the card title
        if typ == "user" and isinstance(m, dict):
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content
                                   if isinstance(c, dict) and c.get("type") == "text")
            if isinstance(content, str) and content.strip() \
                    and not any(n in content for n in _PROMPT_NOISE):
                prompts += 1
                clean = content.strip().replace("\n", " ")
                if not title:
                    title = clean[:120]
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
    if title.startswith("You are labeling a Claude Code coding session"):
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
    }


def _build_history(limit=60):
    files = glob.glob(os.path.join(_PROJECTS_DIR, "*", "*.jsonl"))  # top-level only
    out = []
    for p in files:
        try:
            st = os.stat(p)
        except OSError:
            continue
        k = [st.st_mtime, st.st_size]
        c = _hist_cache.get(p)
        if c and c["k"] == k:
            e = c["e"]
        else:
            e = _scan_history_file(p)
            _hist_cache[p] = {"k": k, "e": e}
        if e:
            out.append(e)
    # drop cache entries for transcripts Claude has pruned
    live = set(files)
    for gone in [p for p in _hist_cache if p not in live]:
        _hist_cache.pop(gone, None)
    out.sort(key=lambda e: e["last"] or 0, reverse=True)
    return out[:limit]


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


def _cwd_for_session(session_id):
    """Look up a session's working dir from its transcript — never trust a
    client-supplied path. Returns None if no such top-level transcript exists."""
    if not _UUID_RE.match(session_id or ""):
        return None
    want = session_id.upper()
    for rows in (_history_result.get("data") or [], _build_history(limit=10000)):
        for e in rows:
            if e["session_id"].upper() == want:
                return e["cwd"] or None
    return None


@writes("resume")
async def api_resume(request):
    """Resume a past session in a fresh iTerm pane: `claude --resume <id>` run in
    that session's own working dir (looked up server-side from the transcript)."""
    import shlex
    body = request.get("_body") or {}
    session_id = (body.get("session_id") or "").strip()
    cwd = await asyncio.to_thread(_cwd_for_session, session_id)
    if not cwd:
        return web.json_response({"error": "no such session"}, status=404)
    if not os.path.isdir(cwd):
        return web.json_response(
            {"error": f"working dir is gone: {cwd}"}, status=409)
    await APP.async_refresh()
    trust_dir(cwd)
    try:
        tab = fleet_tab(APP)
        if tab is None:
            return web.json_response(
                {"error": "no iTerm window open to resume into"}, status=409)
        src, vertical = pick_grid_split(tab)
        sess = await src.async_split_pane(vertical=vertical, before=False)
    except Exception as e:
        return web.json_response(
            {"error": f"could not open pane: {type(e).__name__}: {e}"}, status=500)
    uuid = sess.session_id.upper()
    KNOWN_CLAUDE.add(uuid)
    await sess.async_send_text(
        f"cd {shlex.quote(cwd)} && clear && claude --resume {shlex.quote(session_id)}\n")
    asyncio.create_task(_auto_trust(sess))
    print(f"  [resume] {session_id} → pane {uuid} in {cwd}", flush=True)
    return web.json_response({"uuid": uuid, "session_id": session_id})


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
    from pywebpush import webpush, WebPushException
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


async def ws_pane(request):
    if not authed(request):
        return web.json_response({"error": "locked"}, status=401)
    uuid = request.match_info["uuid"].upper()
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    last = None
    sent_history = False
    try:
        while not ws.closed:
            s = (await all_sessions()).get(uuid)
            if not s:
                await ws.send_json({"gone": True})
                break
            if not sent_history:
                # scrollback is expensive and rarely changes at the top; ship it
                # once so the client can render the whole conversation, then
                # stream only the live screen below it
                hist = await pane_history(s)
                await ws.send_json({"history": hist, "cols": await pane_cols(s)})
                sent_history = True
            txt = await pane_text(s)
            if txt != last:                      # only push on change
                last = txt
                await ws.send_json({"text": txt, "cols": await pane_cols(s),
                                    "prompt": detect_prompt(txt),
                                    "suggest": detect_input(txt)})
            await asyncio.sleep(POLL)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        if not ws.closed:
            await ws.close()
    return ws


async def ws_fleet(request):
    peer = request.remote
    if not authed(request):
        print(f"  [ws/fleet] {peer} REJECTED — no unlocked session", flush=True)
        return web.json_response({"error": "locked"}, status=401)
    print(f"  [ws/fleet] {peer} connected", flush=True)
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
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


def _ts_json(*args):
    import shlex
    cmd = " ".join(shlex.quote(a) for a in (_TAILSCALE, *args))
    try:
        return json.loads(os.popen(f"{cmd} 2>/dev/null").read())
    except Exception:
        return {}


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


def load_peers():
    try:
        return json.loads(PEERS_FILE.read_text())
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


async def api_ping(request):
    """Cross-origin reachability probe. Unauthenticated and CORS-open ON PURPOSE:
    the swapper on device A fetches deviceB/api/ping to light its status dot, a
    cross-origin GET. It reveals only that Dispatch is up, the tailnet hostname
    and this server's own public origin — all already visible on the tailnet —
    and never reads a cookie, so opening it wide costs nothing. `url` lets the
    swapper adopt the peer's authoritative origin. It is the ONLY such route."""
    self_ = next((d for d in tailnet_macs() if d["self"]), {})
    return web.json_response(
        {"dispatch": True, "host": ts_self_host(),
         "url": self_origin(), "name": self_.get("name", "")},
        headers={"Access-Control-Allow-Origin": "*"})


async def main(connection):
    global CONN, APP
    try:
        import icon as icon_art
        icon_art.write_all(HERE / "static" / "icons")
    except Exception as e:
        print(f"  [icons] not regenerated: {type(e).__name__}: {e}", flush=True)
    CONN = connection
    APP = await iterm2.async_get_app(connection)

    app = web.Application()
    app["TOKEN"] = TOKEN
    app.router.add_get("/", index)
    app.router.add_get("/api/fleet", api_fleet)
    app.router.add_get("/api/summary", api_summary)
    app.router.add_post("/api/key", api_key)
    app.router.add_post("/api/send", api_send)
    app.router.add_post("/api/whisper", api_whisper)
    app.router.add_post("/api/upload", api_upload)
    app.router.add_post("/api/submit", api_submit)
    app.router.add_post("/api/spawn", api_spawn)
    app.router.add_get("/api/browse", api_browse)
    app.router.add_post("/api/kill", api_kill)
    app.router.add_post("/api/effort", api_effort)
    app.router.add_post("/api/mode", api_mode)
    app.router.add_post("/api/model", api_model)
    app.router.add_post("/api/cmd", api_cmd)
    app.router.add_get("/api/commands", api_commands)
    app.router.add_get("/api/usage", api_usage)
    app.router.add_get("/api/devices", api_devices)
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
    await web.TCPSite(runner, BIND, PORT).start()

    asyncio.create_task(_notify_watcher())     # push when sessions finish / need input
    asyncio.create_task(_rebuild_history())    # warm the history cache so first open is instant

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
    print(f"  {len(fleet)} Claude panes visible, "
          f"{sum(1 for f in fleet if f['sendable'])} sendable\n", flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    iterm2.run_until_complete(main)
