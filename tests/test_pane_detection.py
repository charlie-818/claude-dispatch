import server as srv

# ── realistic fixtures ───────────────────────────────────────────────────────

NUMBERED_PROMPT = """\
Do you want to proceed?
❯ 1. Yes
  2. Yes, and don't ask again
  3. No
"""

CHECKBOX_PROMPT = """\
Which files should be included?
❯ ☑ src/server.py
  ☐ src/vault.py
  ☐ src/auth.py
(space to toggle, enter to confirm)
"""

INPUT_BOX_GHOST = """\
────────────────────────────────────────────────
❯\xa0fix the bug in the parser
────────────────────────────────────────────────
"""

INPUT_BOX_TYPED = """\
────────────────────────────────────────────────
❯ run the tests
────────────────────────────────────────────────
"""

STATUS_LINE_AUTO = """\
some chat output here
  auto mode on · shift+tab to cycle · esc to interrupt
"""

STATUS_LINE_PLAN = "plan mode on · shift+tab to cycle"
STATUS_LINE_ACCEPT = "accept edits on · shift+tab to cycle"
STATUS_LINE_MANUAL = "manual mode on · shift+tab to cycle"
STATUS_LINE_BYPASS = "bypass permissions on"


# ── marker_provider / job_provider / pane_provider / is_agent_pane ─────────

def test_marker_provider_claude():
    assert srv.marker_provider("some text\nshift+tab to cycle\nmore") == "claude"


def test_marker_provider_codex():
    assert srv.marker_provider("Ask Codex to do anything") == "codex"


def test_marker_provider_grok():
    assert srv.marker_provider("blah · auto-approve blah") == "grok"


def test_marker_provider_none():
    assert srv.marker_provider("just a regular shell prompt $") is None


def test_marker_provider_empty():
    assert srv.marker_provider("") is None
    assert srv.marker_provider(None) is None


def test_job_provider_exact():
    assert srv.job_provider("claude") == "claude"


def test_job_provider_versioned_suffix():
    assert srv.job_provider("grok-1.0.5-macos") == "grok"


def test_job_provider_exe_suffix():
    assert srv.job_provider("claude.exe") == "claude"


def test_job_provider_none_for_shell():
    assert srv.job_provider("bash") is None
    assert srv.job_provider("") is None
    assert srv.job_provider(None) is None


def test_job_provider_no_false_prefix_match():
    # "codexy" starts with "codex" but is not "codex" or "codex-"/"codex."
    assert srv.job_provider("codexy") is None


def test_pane_provider_known_agent_cached(monkeypatch):
    srv.KNOWN_AGENTS.clear()
    srv.KNOWN_AGENTS["ABC"] = "grok"
    try:
        assert srv.pane_provider("abc", "bash", "") == "grok"
    finally:
        srv.KNOWN_AGENTS.clear()


def test_pane_provider_from_job(monkeypatch):
    srv.KNOWN_AGENTS.clear()
    monkeypatch.setattr(srv, "read_fleet_files", lambda: {})
    assert srv.pane_provider("UUID1", "claude", "") == "claude"
    assert srv.KNOWN_AGENTS.get("UUID1") == "claude"
    srv.KNOWN_AGENTS.clear()


def test_pane_provider_from_fleet_file(monkeypatch):
    srv.KNOWN_AGENTS.clear()
    monkeypatch.setattr(srv, "read_fleet_files",
                         lambda: {"UUID2": {"provider": "codex"}})
    assert srv.pane_provider("uuid2", "node", "") == "codex"
    srv.KNOWN_AGENTS.clear()


def test_pane_provider_from_marker(monkeypatch):
    srv.KNOWN_AGENTS.clear()
    monkeypatch.setattr(srv, "read_fleet_files", lambda: {})
    assert srv.pane_provider("UUID3", "bash", "Ask Codex to do anything") == "codex"
    srv.KNOWN_AGENTS.clear()


def test_pane_provider_child_job_fallback(monkeypatch):
    srv.KNOWN_AGENTS.clear()
    monkeypatch.setattr(srv, "read_fleet_files", lambda: {})
    assert srv.pane_provider("UUID4", "node", "") == srv.DEFAULT_PROVIDER
    srv.KNOWN_AGENTS.clear()


def test_pane_provider_plain_shell_is_none(monkeypatch):
    srv.KNOWN_AGENTS.clear()
    monkeypatch.setattr(srv, "read_fleet_files", lambda: {})
    assert srv.pane_provider("UUID5", "bash", "") is None
    srv.KNOWN_AGENTS.clear()


def test_is_agent_pane_true(monkeypatch):
    srv.KNOWN_AGENTS.clear()
    monkeypatch.setattr(srv, "read_fleet_files", lambda: {})
    assert srv.is_agent_pane("UUID6", "claude", "") is True
    srv.KNOWN_AGENTS.clear()


def test_is_agent_pane_false(monkeypatch):
    srv.KNOWN_AGENTS.clear()
    monkeypatch.setattr(srv, "read_fleet_files", lambda: {})
    assert srv.is_agent_pane("UUID7", "bash", "") is False
    srv.KNOWN_AGENTS.clear()


# ── claude_only ──────────────────────────────────────────────────────────────

def test_claude_only_allows_claude():
    srv.KNOWN_AGENTS.clear()
    srv.KNOWN_AGENTS["U1"] = "claude"
    try:
        assert srv.claude_only("u1", "mode") is None
    finally:
        srv.KNOWN_AGENTS.clear()


def test_claude_only_refuses_other():
    srv.KNOWN_AGENTS.clear()
    srv.KNOWN_AGENTS["U2"] = "codex"
    try:
        resp = srv.claude_only("u2", "mode")
        assert resp is not None
        assert resp.status == 409
    finally:
        srv.KNOWN_AGENTS.clear()


def test_claude_only_defaults_to_claude_when_unknown():
    srv.KNOWN_AGENTS.clear()
    assert srv.claude_only("nope", "mode") is None


# ── _is_spinner / _collapse_repeats / clean_history ────────────────────────

def test_is_spinner_matches_elapsed_line():
    assert srv._is_spinner("· Seasoning… (2m 52s)")
    assert srv._is_spinner("✳ Doing thing… (12s)")


def test_is_spinner_false_for_normal_line():
    assert not srv._is_spinner("this is normal chat content")
    assert not srv._is_spinner("")
    assert not srv._is_spinner("   ")


def test_collapse_repeats_collapses_tip_blocks():
    block = ["", "Tip: something helpful", ""]
    lines = block + block + block + ["real content"]
    out = srv._collapse_repeats(lines)
    assert out == block + ["real content"]


def test_collapse_repeats_preserves_non_tip_duplicates():
    block = ["same", "same"]
    lines = block + block
    out = srv._collapse_repeats(lines)
    assert out == lines


def test_clean_history_strips_spinner_and_collapses_tips():
    tip_block = ["", "Tip: try this", ""]
    lines = (tip_block + ["· Working… (1m 2s)"] + tip_block
             + ["some real line"])
    out = srv.clean_history(lines)
    assert "· Working… (1m 2s)" not in out
    assert "some real line" in out
    assert out.count("Tip: try this") == 1


# ── _check_state ─────────────────────────────────────────────────────────────

import pytest


@pytest.mark.parametrize("mark,expected", [
    ("☑", True), ("☒", True), ("◼", True), ("◾", True), ("⬢", True), ("●", True),
    ("☐", False), ("◻", False), ("○", False),
    ("[x]", True), ("[X]", True), ("[✓]", True), ("[·]", True),
    ("[ ]", False), ("[]", False),
])
def test_check_state(mark, expected):
    assert srv._check_state(mark) is expected


# ── read_input_box / detect_input / _input_suggestion ───────────────────────

def test_read_input_box_ghost_suggestion():
    ghost, text = srv.read_input_box(INPUT_BOX_GHOST)
    assert ghost is True
    assert "fix the bug" in text
    assert "in the parser" in text


def test_read_input_box_typed_text():
    ghost, text = srv.read_input_box(INPUT_BOX_TYPED)
    assert ghost is False
    assert text == "run the tests"


def test_read_input_box_empty_when_no_caret():
    ghost, text = srv.read_input_box("no caret anywhere in this text")
    assert ghost is False
    assert text == ""


def test_detect_input_returns_none_for_empty_box():
    assert srv.detect_input("no box here") is None


def test_detect_input_returns_dict_for_typed():
    out = srv.detect_input(INPUT_BOX_TYPED)
    assert out == {"text": "run the tests", "ghost": False}


def test_detect_input_truncates_to_600_chars():
    long_text = "x" * 1000
    text = f"────────\n❯ {long_text}\n────────\n"
    out = srv.detect_input(text)
    assert len(out["text"]) == 600


def test_input_suggestion_ghost():
    kind, sug = srv._input_suggestion(INPUT_BOX_GHOST)
    assert kind == "ghost"
    assert "fix the bug" in sug


def test_input_suggestion_typed():
    kind, sug = srv._input_suggestion(INPUT_BOX_TYPED)
    assert kind == "typed"
    assert sug == "run the tests"


def test_input_suggestion_empty():
    kind, sug = srv._input_suggestion("nothing here")
    assert kind == "empty"
    assert sug == ""


# ── _last_numbered_run / _last_unnumbered_run / _is_option_row ─────────────

def test_last_numbered_run_finds_run_with_caret():
    lines = NUMBERED_PROMPT.splitlines()
    run = srv._last_numbered_run(lines)
    assert run is not None
    assert len(run) == 3
    # exactly one row has the caret selected
    assert sum(1 for o in run if o[3]) == 1


def test_last_numbered_run_none_for_plain_list():
    lines = "1. Yes\n2. No\n".splitlines()  # no caret on any row
    assert srv._last_numbered_run(lines) is None


def test_last_unnumbered_run_finds_checkbox_run():
    lines = CHECKBOX_PROMPT.splitlines()
    run = srv._last_unnumbered_run(lines)
    assert run is not None
    assert len(run) == 3
    assert sum(1 for o in run if o[3]) == 1


def test_is_option_row_numbered():
    assert srv._is_option_row("❯ 1. Yes")


def test_is_option_row_checkbox():
    assert srv._is_option_row("❯ ☑ Option A")
    assert srv._is_option_row("  ☐ Option B")


def test_is_option_row_false_for_prose():
    assert not srv._is_option_row("just some regular text")


# ── detect_prompt ────────────────────────────────────────────────────────────

def test_detect_prompt_numbered_permission():
    out = srv.detect_prompt(NUMBERED_PROMPT)
    assert out is not None
    assert out["question"] == "Do you want to proceed?"
    assert out["multi"] is False
    assert len(out["options"]) == 3
    assert out["options"][0]["label"] == "Yes"
    assert out["options"][0]["selected"] is True


def test_detect_prompt_checkbox_multiselect():
    out = srv.detect_prompt(CHECKBOX_PROMPT)
    assert out is not None
    assert out["multi"] is True
    assert len(out["options"]) == 3
    checked = [o for o in out["options"] if o["checked"]]
    assert len(checked) == 1
    assert "src/server.py" in checked[0]["label"]


def test_detect_prompt_none_for_plain_text():
    assert srv.detect_prompt("just some ordinary chat output\nno options here") is None


def test_detect_prompt_question_len_capped():
    long_q = "Why " * 80 + "?"
    text = f"{long_q}\n❯ 1. Yes\n  2. No\n"
    out = srv.detect_prompt(text)
    assert out is not None
    assert len(out["question"]) <= 160


# ── human_prompt ─────────────────────────────────────────────────────────────

def test_human_prompt_passes_real_text():
    assert srv.human_prompt("fix the login bug please") == "fix the login bug please"


def test_human_prompt_strips_system_reminder_wrapper():
    txt = "<system-reminder>ignore me</system-reminder>do the thing"
    assert srv.human_prompt(txt) == "do the thing"


def test_human_prompt_rejects_noise():
    assert srv.human_prompt("Caveat: something something") == ""
    assert srv.human_prompt("<command-name>ls</command-name>") == ""


def test_human_prompt_empty_input():
    assert srv.human_prompt("") == ""
    assert srv.human_prompt(None) == ""


def test_human_prompt_strips_channel_tags_but_keeps_content():
    # _WRAPPERS only removes the <channel ...> / </channel> tags themselves,
    # not the text they wrap.
    txt = "<channel>relayed</channel>hello there"
    assert srv.human_prompt(txt) == "relayedhello there"


# ── _short_action ────────────────────────────────────────────────────────────

def test_short_action_bash_command():
    assert srv._short_action("Bash", {"command": "ls -la /tmp\nsecond line"}) == "ls -la /tmp"


def test_short_action_file_tool_basename():
    assert srv._short_action("Edit", {"file_path": "/a/b/c/server.py"}) == "server.py"


def test_short_action_other_query():
    assert srv._short_action("Grep", {"pattern": "foo bar"}) == "foo bar"


def test_short_action_empty_when_nothing_matches():
    assert srv._short_action("Unknown", {}) == ""


# ── _safe_name ───────────────────────────────────────────────────────────────

def test_safe_name_strips_path():
    assert srv._safe_name("/etc/passwd") == "passwd"


def test_safe_name_replaces_bad_chars():
    assert srv._safe_name("my file!@#.txt") == "my_file___.txt"


def test_safe_name_default_when_empty():
    assert srv._safe_name("") == "file"
    assert srv._safe_name(None) == "file"


def test_safe_name_truncates_to_120():
    assert len(srv._safe_name("a" * 200)) <= 120


# ── detect_mode ──────────────────────────────────────────────────────────────

def test_detect_mode_bypass():
    assert srv.detect_mode(STATUS_LINE_BYPASS) == "bypass"


def test_detect_mode_plan():
    assert srv.detect_mode(STATUS_LINE_PLAN) == "plan"


def test_detect_mode_accept():
    assert srv.detect_mode(STATUS_LINE_ACCEPT) == "accept"


def test_detect_mode_auto():
    assert srv.detect_mode(STATUS_LINE_AUTO) == "auto"


def test_detect_mode_manual():
    assert srv.detect_mode(STATUS_LINE_MANUAL) == "manual"


def test_detect_mode_none_when_absent():
    assert srv.detect_mode("just some chat text with no mode line") is None
