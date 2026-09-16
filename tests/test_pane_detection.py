import asyncio
import json
import pathlib
from unittest.mock import AsyncMock

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

# AskUserQuestion as Claude Code 2.1 draws it: a tab strip, the question, each
# option with an indented description, a rule, then the "Chat about this" row
ASK_PROMPT = """\
⏺ Use the AskUserQuestion tool

 ☐ Colour
Which colour?
❯ 1. Red
     warm
  2. Blue
     cool
  3. Type something.
──────────────────────────────────────────────
  4. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
"""

# a permission prompt in a narrow pane: the long label hard-wraps onto lines
# indented to the label column — those are the SAME label, not a description
WRAPPED_PROMPT = """\
Do you want to proceed?
❯ 1. Yes
  2. Yes, and don't ask again for
     npm commands in /tmp
  3. No
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

def test_detect_prompt_ask_user_question():
    out = srv.detect_prompt(ASK_PROMPT)
    assert out is not None
    assert out["question"] == "Which colour?"
    assert out["multi"] is False
    labels = [o["label"] for o in out["options"]]
    assert labels == ["Red", "Blue", "Type something.", "Chat about this"]
    assert out["options"][0]["desc"] == "warm"
    assert out["options"][1]["desc"] == "cool"
    assert "desc" not in out["options"][2]
    assert out["options"][0]["selected"] is True
    assert [o["key"] for o in out["options"]] == ["1", "2", "3", "4"]


def test_detect_prompt_wrapped_label_is_rejoined():
    out = srv.detect_prompt(WRAPPED_PROMPT)
    assert out is not None
    assert out["options"][1]["label"] == "Yes, and don't ask again for npm commands in /tmp"
    assert "desc" not in out["options"][1]


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


@pytest.mark.parametrize("question,labels", [
    ("Select model", ["gpt-5.4", "gpt-5.3-codex", "gpt-5.2-codex"]),
    ("Select reasoning effort", ["Low", "Medium", "High"]),
    ("Select permissions", ["Read Only", "Default", "Full Access"]),
])
def test_detect_prompt_codex_picker(question, labels):
    text = question + "\n" + "\n".join(
        f"{'›' if i == 3 else ' '} {i}. {label}"
        for i, label in enumerate(labels, 1))
    out = srv.detect_prompt(text)
    assert out == {
        "question": question, "multi": False,
        "options": [
            {"key": str(i), "label": label, "selected": i == 3,
             "checked": False, "index": i - 1}
            for i, label in enumerate(labels, 1)],
    }
    assert srv.detect_prompt(text.replace("›", " ")) is None


def test_detect_prompt_codex_wrapped_description():
    out = srv.detect_prompt(
        "Select permissions\n"
        "  1. Read Only\n"
        "     Require approval to edit\n"
        "     files or run commands.\n"
        "› 2. Default\n"
        "  3. Full Access\n"
        "Press enter to select or esc to dismiss\n")
    assert out["options"][0]["label"] == "Read Only"
    assert out["options"][0]["desc"] == "Require approval to edit files or run commands."


def test_codex_permissions_picker_single_cancel():
    out = srv.detect_prompt(
        "Select permissions\n"
        "  1. Read Only\n"
        "     Require approval to edit\n"
        "     files or run commands.\n"
        "› 2. Default\n"
        "  3. Full Access\n"
        "Press enter to select or esc to dismiss\n", "codex")
    assert out["kind"] == "picker"
    assert [o["label"] for o in out["options"]] == ["Read Only", "Default", "Full Access"]
    assert out["actions"] == ["choose", "cancel"]
    assert not any("cancel" in o["label"].lower() or "esc" in o["label"].lower()
                   for o in out["options"])


FULL_ACCESS_CONFIRM = (
    "Enable full access?\n"
    "Allow Codex to read, write, and\n"
    "computer and run commands with network, without your approval.\n"
    "Exercise caution when enabling full access. This significantly\n"
    "increases the risk of data loss, leaks, or unexpected behavior.\n"
    "› 1. Yes, continue anyway\n"
    "     Apply full access for this session\n"
    "  2. Cancel\n"
    "     Go back without enabling full access\n"
    "Press enter to confirm or esc to go back\n"
)


def test_codex_full_access_confirm_is_picker():
    out = srv.detect_prompt(FULL_ACCESS_CONFIRM, "codex")
    assert out["kind"] == "picker"
    assert out["question"] == "Enable full access?"
    assert [o["label"] for o in out["options"]] == ["Yes, continue anyway", "Cancel"]
    assert out["options"][0]["desc"] == "Apply full access for this session"
    assert out["options"][1]["desc"] == "Go back without enabling full access"
    assert out["actions"] == ["choose", "cancel"]


class CommandRequest(dict):
    def __init__(self, body=None, query=None):
        super().__init__()
        self.body = body or {}
        self.query = query or {}

    async def json(self):
        return self.body


@pytest.fixture
def command_api(monkeypatch):
    monkeypatch.setattr(srv.auth, "same_origin", lambda request: True)
    monkeypatch.setattr(srv.auth, "unlocked", lambda request: {})
    monkeypatch.setattr(srv.auth, "audit", lambda *args: None)
    monkeypatch.setattr(srv, "note_activity", lambda: None)
    monkeypatch.setattr(srv, "KNOWN_AGENTS", {})
    monkeypatch.setattr(srv, "read_fleet_files", lambda: {})
    monkeypatch.setattr(srv, "pane_text", AsyncMock(return_value=""))
    monkeypatch.setattr(srv.asyncio, "sleep", AsyncMock())


@pytest.mark.parametrize("provider,expected", [
    (None, srv.COMMANDS), ("claude", srv.COMMANDS),
    ("codex", srv.CODEX_COMMANDS),
    ("grok", {"clear": ("/clear", "wipe context")}),
])
def test_commands_list_by_provider(command_api, provider, expected):
    query = {} if provider is None else {"provider": provider}
    response = asyncio.run(srv.api_commands(CommandRequest(query=query)))
    assert response.status == 200
    assert json.loads(response.text) == {"commands": [
        {"id": key, "cmd": value[0], "desc": value[1]}
        for key, value in expected.items()]}


@pytest.mark.parametrize("provider,command,expected", [
    ("codex", "model", ["/model", "\r"]),
    ("codex", "permissions", ["/permissions", "\r"]),
    ("claude", "context", ["/context", "\r", "\r"]),
    ("codex", "clear", ["/clear", "\r"]),
    ("claude", "permissions", []),
    ("grok", "model", []),
    ("bash", "model", []),
])
def test_command_routes_by_actual_pane(command_api, monkeypatch, provider, command, expected):
    pane = AsyncMock()
    pane.async_get_variable.return_value = provider
    monkeypatch.setattr(srv, "all_sessions", AsyncMock(return_value={"PANE": pane}))
    request = CommandRequest({"uuid": "pane", "cmd": command, "provider": "claude"})
    response = asyncio.run(srv.api_cmd(request))
    assert response.status == (200 if expected else 403 if provider == "bash" else 400)
    assert [call.args[0] for call in pane.async_send_text.await_args_list] == expected
    if expected:
        assert json.loads(response.text) == {"ok": True, "cmd": expected[0]}
        assert srv.asyncio.sleep.await_args_list[0].args == (0.9,)


def test_command_missing_pane(command_api, monkeypatch):
    monkeypatch.setattr(srv, "all_sessions", AsyncMock(return_value={}))
    response = asyncio.run(srv.api_cmd(CommandRequest({"uuid": "missing", "cmd": "model"})))
    assert response.status == 404


CODEX_CATALOG = [{"id": "gpt-test", "label": "GPT Test",
                  "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
                  "default_effort": "medium"}]
CODEX_IDLE = "OpenAI Codex\n› Ask Codex to do anything\n  100% context left"
CODEX_MODEL_PICKER = "Select Model and Effort\n› 1. gpt-other\n  2. gpt-test (current)  Reliable agentic workhorse."
CODEX_SELECT_MODEL = (
    "Select Model\n"
    "Pick a quick auto mode or browse all models.\n"
    "  1. codex-auto-fast\n"
    "  2. codex-auto-balanced\n"
    "› 3. All models\n"
    "     Choose a specific model and reasoning level (current: gpt-test)\n")
CODEX_EFFORT_PICKER = ("Select Reasoning Level for gpt-test\n"
                       "  1. High\n› 2. Medium (default)\n  3. Low\n"
                       "  4. Extra high\n  5. More reasoning…")
CODEX_ADVANCED_PICKER = ("Advanced Reasoning\n⚠ Consumes usage limits faster\n"
                         "› 1. Ultra  For demanding work using multiple agents · highest usage\n"
                         "  2. Max    For difficult problems when quality matters more than speed ·\n"
                         "            higher usage")


@pytest.mark.parametrize("placeholder", ["", *sorted(srv._CODEX_PLACEHOLDERS)])
def test_codex_idle_accepts_empty_composer(placeholder):
    assert srv._codex_idle(f"OpenAI Codex\n› {placeholder}\n  100% context left")


@pytest.mark.parametrize("screen", [
    "", "OpenAI Codex\n100% context left", "› draft message",
    "›\n  draft on next line\n\n100% context left",
    "› Ask Codex to do anything\n  continued draft\n\n100% context left",
    "Working (esc to interrupt)\n›", CODEX_MODEL_PICKER,
    CODEX_EFFORT_PICKER, CODEX_ADVANCED_PICKER,
    "Select permissions\n› 1. Read only\n  2. Full access\nEnter to confirm",
    "Question 1/1\nShare details.\n› Type your answer (optional)\n"
    "enter to submit answer | tab to navigate questions",
])
def test_codex_idle_rejects_drafts_busy_and_pickers(screen):
    assert not srv._codex_idle(screen)


def test_codex_idle_ignores_numbered_transcript():
    assert srv._codex_idle("Previous answer:\n1. First step\n2. Second step\n" + CODEX_IDLE)


def test_codex_idle_ignores_historical_interrupt_and_picker():
    screen = (
        "Working (esc to interrupt)\n"
        "Select Model and Effort\n"
        "  1. gpt-old\n"
        "  2. gpt-test\n"
        "Model changed to gpt-test medium\n"
        + CODEX_IDLE
    )
    assert srv._codex_idle(screen)


def test_codex_idle_ignores_submitted_prompt_then_error():
    screen = "› go\n■ You've hit your usage limit. Upgrade to Pro\n" + CODEX_IDLE
    assert srv._codex_idle(screen)


@pytest.mark.parametrize("screen", [
    "• Working (12s • esc to interrupt)»Ask Codex to do anything",
    "◦Applying both edits(52s • esc to interrupt)»Ask Codex to do anything",
    "• Starting script creation (1m 00s • esc to interrupt)\n›",
    "• Working (1h 00m 00s • esc to interrupt)\ngpt-6-astra · ~/src",
    "Working (esc to interrupt)\n›",
])
def test_codex_busy_matches_live_progress(screen):
    assert srv._codex_busy(screen)
    assert srv.detect_running("codex", screen) is True


def test_codex_busy_ignores_question_footer():
    screen = (
        "Question 1/1 (1 unanswered)\n"
        "Share details.\n"
        "› Type your answer (optional)\n"
        "enter to submit answer | esc to interrupt"
    )
    assert not srv._codex_busy(screen)


def test_codex_busy_ignores_historical_progress():
    screen = "• Working (12s • esc to interrupt)\n" + CODEX_IDLE
    assert not srv._codex_busy(screen)
    assert srv.detect_running("codex", screen) is False


def test_codex_journal_open_turn(tmp_path):
    p = tmp_path / "rollout.jsonl"
    p.write_text(json.dumps({"type": "event_msg",
                             "payload": {"type": "task_started"}}) + "\n")
    assert srv.detect_running("codex", "OpenAI Codex\nstill going", str(p)) is True
    p.write_text(p.read_text() + json.dumps(
        {"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
    assert srv.detect_running("codex", "OpenAI Codex\nstill going", str(p)) is False


@pytest.mark.parametrize("end", ["task_complete", "turn_aborted"])
def test_codex_open_turn_beats_idle_composer_until_turn_ends(tmp_path, end):
    p = tmp_path / "rollout.jsonl"
    p.write_text(json.dumps({"type": "event_msg",
                             "payload": {"type": "task_started"}}) + "\n")
    assert srv.detect_running("codex", CODEX_IDLE, str(p)) is True
    p.write_text(p.read_text() + json.dumps(
        {"type": "event_msg", "payload": {"type": end}}) + "\n")
    assert srv.detect_running("codex", CODEX_IDLE, str(p)) is False


@pytest.fixture
def codex_controls(command_api, monkeypatch):
    monkeypatch.setattr(srv, "_codex_models", AsyncMock(return_value=CODEX_CATALOG))
    monkeypatch.setattr(srv, "_PANE_WRITES", set())


def codex_screens(monkeypatch, screens):
    pane = AsyncMock()
    pane.async_get_variable.return_value = "codex"
    pending = iter(screens)
    current = screens[-1]

    async def read(_):
        nonlocal current
        current = next(pending, current)
        return current

    monkeypatch.setattr(srv, "pane_text", read)
    monkeypatch.setattr(srv, "all_sessions", AsyncMock(return_value={"PANE": pane}))
    return pane


@pytest.mark.parametrize("model,level,keys", [
    ("gpt-test", "high", ["/model", "\r", "2", "1"]),
    (None, "low", ["/model", "\r", "2", "3"]),
    ("gpt-test", None, ["/model", "\r", "2", "2"]),
    ("gpt-test", "ultra", ["/model", "\r", "2", "5", "1"]),
    (None, "max", ["/model", "\r", "2", "5", "2"]),
])
def test_codex_direct_selection(codex_controls, monkeypatch, model, level, keys):
    wanted = level or "medium"
    final = f"Model changed to gpt-test {wanted}\n{CODEX_IDLE}"
    idle = CODEX_IDLE.replace("Ask Codex to do anything", "Explain this codebase")
    screens = [idle, CODEX_MODEL_PICKER, CODEX_EFFORT_PICKER]
    if wanted in ("max", "ultra"):
        screens.append(CODEX_ADVANCED_PICKER)
    screens.extend([screens[-1], final])
    pane = codex_screens(monkeypatch, screens)
    response = asyncio.run(srv._codex_change(pane, model=model, level=level))
    assert response.status == 200
    assert json.loads(response.text) == {
        "ok": True, "model": "gpt-test", "level": wanted, "prompt": None}
    assert [call.args[0] for call in pane.async_send_text.await_args_list] == keys


CODEX_EFFORT_ASK = CODEX_EFFORT_PICKER + "\nPress enter to confirm or esc to cancel"


def test_codex_change_opens_all_models_from_select_model(codex_controls, monkeypatch):
    final = f"Model changed to gpt-test medium\n{CODEX_IDLE}"
    pane = codex_screens(monkeypatch, [
        CODEX_IDLE, CODEX_SELECT_MODEL, CODEX_MODEL_PICKER, CODEX_EFFORT_PICKER,
        CODEX_EFFORT_PICKER, final])
    response = asyncio.run(srv._codex_change(pane, model="gpt-test", level="medium"))
    assert response.status == 200
    assert [call.args[0] for call in pane.async_send_text.await_args_list] == [
        "/model", "\r", "3", "2", "2"]


def test_codex_change_dismisses_leftover_picker(codex_controls, monkeypatch):
    final = CODEX_MODEL_PICKER + f"\nModel changed to gpt-test medium\n{CODEX_IDLE}"
    pane = codex_screens(monkeypatch, [
        CODEX_IDLE, CODEX_MODEL_PICKER, CODEX_EFFORT_PICKER,
        CODEX_EFFORT_PICKER, final])
    response = asyncio.run(srv._codex_change(pane, model="gpt-test", level="medium"))
    assert response.status == 200
    assert json.loads(response.text)["prompt"] is None
    keys = [call.args[0] for call in pane.async_send_text.await_args_list]
    assert keys[:4] == ["/model", "\r", "2", "2"]
    assert keys[4:] and all(k == "\x1b" for k in keys[4:])


def test_codex_effort_click_keeps_current_model_and_asks(codex_controls, monkeypatch):
    pane = codex_screens(monkeypatch, [CODEX_IDLE, CODEX_MODEL_PICKER, CODEX_EFFORT_ASK])
    response = asyncio.run(srv._codex_change(pane, ask=True, uuid="PANE"))
    assert response.status == 200
    body = json.loads(response.text)
    assert body["ok"] is True
    assert body["model"] == "gpt-test"
    assert "level" not in body
    assert body["prompt"]["kind"] == "picker"
    assert body["prompt"]["question"] == "Select Reasoning Level for gpt-test"
    assert [o["label"] for o in body["prompt"]["options"]] == [
        "High", "Medium (default)", "Low", "Extra high", "More reasoning…"]
    assert [call.args[0] for call in pane.async_send_text.await_args_list] == ["/model", "\r", "2"]


def test_codex_effort_ask_unknown_model_uses_current(codex_controls, monkeypatch):
    pane = codex_screens(monkeypatch, [CODEX_IDLE, CODEX_MODEL_PICKER, CODEX_EFFORT_ASK])
    response = asyncio.run(srv._codex_change(pane, model="missing", ask=True, uuid="PANE"))
    assert response.status == 200
    assert json.loads(response.text)["model"] == "gpt-test"
    assert [call.args[0] for call in pane.async_send_text.await_args_list] == ["/model", "\r", "2"]


def test_api_effort_without_level_asks(command_api, monkeypatch):
    asked = {}

    async def fake(s, model=None, level=None, ask=False, uuid=None):
        asked.update(model=model, level=level, ask=ask, uuid=uuid)
        return srv.web.json_response({"ok": True, "model": "gpt-test", "prompt": {"kind": "picker"}})

    monkeypatch.setattr(srv, "_codex_change", fake)
    monkeypatch.setattr(srv, "all_sessions", AsyncMock(return_value={"PANE": AsyncMock()}))
    monkeypatch.setattr(srv, "is_agent_pane", lambda *a: True)
    monkeypatch.setattr(srv, "provider_of", lambda uuid: "codex")
    monkeypatch.setattr(srv, "pane_text", AsyncMock(return_value=CODEX_IDLE))
    response = asyncio.run(srv.api_effort(CommandRequest({"uuid": "pane", "model": "gpt-test"})))
    assert response.status == 200
    assert asked == {"model": "gpt-test", "level": None, "ask": True, "uuid": "PANE"}


@pytest.mark.parametrize("model,level", [("missing", "high"), ("gpt-test", "bogus"),
                                         ("gpt-test", []), ("gpt-test", "ultracode")])
def test_codex_invalid_selection_does_not_type(codex_controls, monkeypatch, model, level):
    pane = codex_screens(monkeypatch, [CODEX_IDLE])
    response = asyncio.run(srv._codex_change(pane, model=model, level=level))
    assert response.status == 400
    pane.async_send_text.assert_not_awaited()


@pytest.mark.parametrize("screen", ["Working (esc to interrupt)\n›",
                                    CODEX_MODEL_PICKER, "shell $"])
def test_codex_busy_does_not_type(codex_controls, monkeypatch, screen):
    pane = codex_screens(monkeypatch, [screen])
    response = asyncio.run(srv._codex_change(pane, model="gpt-test"))
    assert response.status == 409
    pane.async_send_text.assert_not_awaited()


def test_codex_change_clears_composer_draft(codex_controls, monkeypatch):
    draft = "› leftover draft\n  100% context left"
    final = f"Model changed to gpt-test medium\n{CODEX_IDLE}"
    pane = codex_screens(monkeypatch, [
        draft, CODEX_IDLE, CODEX_MODEL_PICKER, CODEX_EFFORT_PICKER,
        CODEX_EFFORT_PICKER, final])
    response = asyncio.run(srv._codex_change(pane, model="gpt-test", level="medium"))
    assert response.status == 200
    assert [call.args[0] for call in pane.async_send_text.await_args_list] == [
        "\x15", "/model", "\r", "2", "2"]


def test_codex_missing_row_cleans_owned_picker(codex_controls, monkeypatch):
    picker = "Select Model and Effort\n› 1. gpt-other\n  2. gpt-absent"
    pane = codex_screens(monkeypatch, [CODEX_IDLE, picker])
    response = asyncio.run(srv._codex_change(pane, model="gpt-test"))
    assert response.status == 409
    keys = [call.args[0] for call in pane.async_send_text.await_args_list]
    assert keys[:2] == ["/model", "\r"]
    assert keys[2:] and all(k == "\x1b" for k in keys[2:])


def test_codex_timeout_never_reports_success(codex_controls, monkeypatch):
    pane = codex_screens(monkeypatch, [CODEX_IDLE, CODEX_MODEL_PICKER,
                                       CODEX_EFFORT_PICKER, CODEX_EFFORT_PICKER, CODEX_IDLE])
    response = asyncio.run(srv._codex_change(pane, model="gpt-test", level="high"))
    assert response.status == 409
    assert [call.args[0] for call in pane.async_send_text.await_args_list] == ["/model", "\r", "2", "1"]


def test_codex_stale_completion_not_success(codex_controls, monkeypatch):
    stale = "Model changed to gpt-test high\n"
    pane = codex_screens(monkeypatch, [CODEX_IDLE, CODEX_MODEL_PICKER,
                                       CODEX_EFFORT_PICKER, stale + CODEX_EFFORT_PICKER,
                                       stale + CODEX_IDLE])
    response = asyncio.run(srv._codex_change(pane, model="gpt-test", level="high"))
    assert response.status == 409


@pytest.mark.parametrize("handler,body", [
    (srv.api_key, {"key": "enter"}), (srv.api_send, {"text": "hello"}),
    (srv.api_cmd, {"cmd": "model"}), (srv.api_model, {"model": "gpt-test"}),
    (srv.api_effort, {"level": "high"}),
])
def test_codex_transaction_rejects_concurrent_writes(codex_controls, monkeypatch, handler, body):
    pane = codex_screens(monkeypatch, [CODEX_IDLE])
    srv._PANE_WRITES.add("PANE")
    response = asyncio.run(handler(CommandRequest({"uuid": "pane", **body})))
    assert response.status == 409
    pane.async_send_text.assert_not_awaited()


def test_codex_model_catalog_filters_and_caches(command_api, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    model = {"slug": "gpt-test", "display_name": "GPT Test", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "high", "description": "More"}],
             "default_reasoning_level": "high"}
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (json.dumps({"models": [model, {**model, "visibility": "hide"}]}).encode(), b"")
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(srv.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(srv, "agent_bin", lambda provider: "/bin/codex")
    monkeypatch.setattr(srv, "_CODEX_MODELS", {"at": 0, "models": []})
    monkeypatch.setattr(srv, "_CODEX_MODEL_LOCK", asyncio.Lock())

    async def run():
        first = await srv.api_codex_models(CommandRequest())
        second = await srv.api_codex_models(CommandRequest())
        assert first.text == second.text
        return first

    response = asyncio.run(run())
    assert response.status == 200
    assert json.loads(response.text) == {"models": [{"id": "gpt-test", "label": "GPT Test",
                                                     "efforts": ["high"], "default_effort": "high"}]}
    assert spawn.await_count == 1
    assert spawn.await_args.args == ("/bin/codex", "debug", "models")
    child_path = spawn.await_args.kwargs["env"]["PATH"].split(":")
    assert child_path[:4] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    assert "/opt/homebrew/bin" in child_path
    assert "/usr/local/bin" in child_path


def test_codex_catalog_failure_is_unavailable(command_api, monkeypatch):
    monkeypatch.setattr(srv, "_codex_models", AsyncMock(side_effect=RuntimeError("failed")))
    response = asyncio.run(srv.api_codex_models(CommandRequest()))
    assert response.status == 503


CODEX_QUESTION_SCREEN = """Question 1/2 (2 unanswered)
Choose a color.

› 1. Red (Recommended)  Select red as the test color.
  2. Blue               Select blue as the test color.
  3. None of the above  Optionally, add details in notes (tab).

tab to add notes | enter to submit answer | ←/→ to navigate questions
esc to interrupt"""
CODEX_APPROVAL_SCREEN = """Would you like to run the following command?

Thread: Robie [explorer]

$ echo hi

› 1. Yes, proceed (y)
  2. No, and tell Codex what to do differently (esc)

Press enter to confirm or esc to cancel or o to open thread"""


def test_codex_question_schema_and_episode(monkeypatch):
    monkeypatch.setattr(srv, "_CODEX_PROMPT_EPISODES", {})
    first = srv.detect_prompt(CODEX_QUESTION_SCREEN, "codex", "PANE")
    assert first["kind"] == "question"
    assert first["question"] == "Choose a color."
    assert (first["question_index"], first["question_count"]) == (1, 2)
    assert first["options"][0]["label"] == "Red (Recommended)"
    assert first["options"][0]["desc"] == "Select red as the test color."
    notes = CODEX_QUESTION_SCREEN.replace("tab to add notes", "› hello 世界\n  next line\n\ntab or esc to clear notes")
    second = srv.detect_prompt(notes.replace("(2 unanswered)", "(1 unanswered)"), "codex", "PANE")
    assert second["id"] == first["id"]
    assert second["input"]["text"] == "hello 世界\nnext line"
    assert srv.detect_input(notes, "codex") is None
    assert srv.detect_prompt("Questions 2/2 answered\n› Ask Codex to do anything", "codex", "PANE") is None
    assert srv.detect_prompt(CODEX_QUESTION_SCREEN, "codex", "PANE")["id"] != first["id"]


def test_codex_approval_preserves_command():
    prompt = srv.detect_prompt(CODEX_APPROVAL_SCREEN, "codex")
    assert prompt["kind"] == "approval"
    assert "$ echo hi" in prompt["context"]
    assert "Thread: Robie [explorer]" in prompt["context"]
    assert prompt["options"][1]["label"] == "No, and tell Codex what to do differently (esc)"
    assert prompt["actions"] == ["choose", "cancel"]


@pytest.mark.parametrize("screen", [
    "Question 1/2\nChoose a color.\n1. Red\n2. Blue",
    "Questions 2/2 answered\nanswer: Blue\nnote: Good\n› Ask Codex to do anything",
    CODEX_QUESTION_SCREEN + "\nQuestions 2/2 answered\n› Ask Codex to do anything",
])
def test_codex_answered_prose_is_not_prompt(screen):
    assert srv.detect_prompt(screen, "codex") is None


def test_codex_freeform_and_composer():
    freeform = "Question 1/1 (1 unanswered)\nShare details.\n› Type your answer (optional)\nenter to submit answer | esc to interrupt"
    prompt = srv.detect_prompt(freeform, "codex")
    assert prompt["options"] == []
    assert prompt["input"] == {"allowed": True, "visible": True, "text": "", "placeholder": "Type your answer (optional)"}
    assert srv.detect_input("› first line\n  second line\n\n100% context left", "codex") == {"text": "first line\nsecond line", "ghost": False}
    assert srv.detect_input("› Ask Codex to do anything\n100% context left", "codex")["ghost"] is True


def test_codex_animation_composer_is_not_suggestion():
    screen = "› old text\n\n› ⠁ ⠂ ⠄\n100% context left"
    assert srv.detect_input(screen, "codex") is None
    assert srv.detect_prompt(screen, "codex") is None


def test_codex_placeholder_ignores_animation_tail():
    screen = "› Ask Codex to do anything\n  ⠁ ⠂ ⠄\n100% context left"
    assert srv.detect_input(screen, "codex") == {"text": "Ask Codex to do anything", "ghost": True}


@pytest.mark.parametrize("placeholder", sorted(srv._CODEX_PLACEHOLDERS))
def test_codex_placeholder_ignores_same_line_animation_frames(placeholder):
    for frame in ("", " ⠁", " ⠂ ⠄", " ⠀ ⣿", ""):
        screen = f"› {placeholder}{frame}\n100% context left"
        assert srv.detect_input(screen, "codex") == {"text": placeholder, "ghost": True}


def test_codex_animation_between_continuations_is_omitted():
    screen = "› first line\n  second line\n  ⠁ ⠂ ⠄\n  third line\n100% context left"
    assert srv.detect_input(screen, "codex") == {"text": "first line\nsecond line\nthird line", "ghost": False}


@pytest.mark.parametrize("draft", [
    "你好 ⠁\nrésumé ⠂",
    "Ask Codex to do anything ⠁ please",
    "Ask Codex to do anything ...",
    "Ask Codex to do anything · • ⠁",
    "Ask Codex to do anything⠁",
    "Add notes or text ⠁\nréel brouillon",
])
def test_codex_mixed_braille_text_is_preserved(draft):
    screen = "› " + draft.replace("\n", "\n  ") + "\n100% context left"
    assert srv.detect_input(screen, "codex") == {"text": draft, "ghost": False}


def test_codex_question_notes_ignore_animation_tail():
    screen = CODEX_QUESTION_SCREEN.replace(
        "tab to add notes", "› useful notes\n  ⠁ ⠂ ⠄\n\ntab or esc to clear notes")
    prompt = srv.detect_prompt(screen, "codex")
    assert prompt["input"]["text"] == "useful notes"


@pytest.fixture
def native_question(command_api, monkeypatch):
    monkeypatch.setattr(srv, "_CODEX_PROMPT_EPISODES", {})
    monkeypatch.setattr(srv, "_PANE_WRITES", set())
    state = {"question": 1, "selected": 0, "notes": False, "text": "", "done": False, "sent": [], "stuck": False}

    def screen():
        if state["done"]:
            return "Questions 2/2 answered\n› Ask Codex to do anything"
        value = CODEX_QUESTION_SCREEN.replace("Question 1/2", f"Question {state['question']}/2")
        if state["question"] == 2:
            value = value.replace("Choose a color.", "Choose a size.").replace("submit answer", "submit all")
        value = value.replace("› 1.", "  1.").replace(f"  {state['selected'] + 1}.", f"› {state['selected'] + 1}.")
        if state["notes"]:
            value = value.replace("tab to add notes", "› " + (state["text"].replace("\n", "\n  ") or "Add notes") + "\n\ntab or esc to clear notes")
        return value

    async def send(value):
        state["sent"].append(value)
        if state["stuck"]:
            return
        if value == "\x1b[B":
            state["selected"] += 1
        elif value == "\x1b[A":
            state["selected"] -= 1
        elif value == "\t":
            state["notes"] = not state["notes"]
            state["text"] = ""
        elif value.startswith("\x1b[200~"):
            state["text"] = value[6:-6]
        elif value in ("\x10", "\x0e"):
            state["question"] += -1 if value == "\x10" else 1
        elif value == "\x03":
            if state["notes"]:
                state.update(notes=False, text="")
            else:
                state["done"] = True
        elif value == "\r":
            if state["question"] == 2:
                state["done"] = True
            else:
                state.update(question=2, notes=False, text="", selected=0)

    pane = AsyncMock()
    pane.async_get_variable.return_value = "codex"
    pane.async_send_text.side_effect = send
    monkeypatch.setattr(srv, "all_sessions", AsyncMock(return_value={"PANE": pane}))
    monkeypatch.setattr(srv, "pane_text", AsyncMock(side_effect=lambda _: screen()))

    def request(action="submit", **extra):
        prompt = srv.detect_prompt(screen(), "codex", "PANE")
        return CommandRequest({"uuid": "pane", "prompt_id": prompt["id"], "action": action, **extra})

    return state, request, pane


def test_codex_question_submit_notes_and_final(native_question):
    state, request, _ = native_question
    response = asyncio.run(srv.api_prompt(request(option_index=1, text="你好\nsecond line")))
    assert response.status == 200
    assert json.loads(response.text)["prompt"]["question_index"] == 2
    assert state["sent"] == ["\x1b[B", "\t", "\x1b[200~你好\nsecond line\x1b[201~", "\r"]
    response = asyncio.run(srv.api_prompt(request(option_index=0)))
    assert json.loads(response.text) == {"ok": True, "prompt": None}


def test_codex_none_above_opens_notes_before_submit(native_question):
    state, request, _ = native_question
    response = asyncio.run(srv.api_prompt(request(option_index=2, text="")))
    assert response.status == 200
    assert state["sent"] == ["\x1b[B", "\x1b[B", "\t", "\r"]


def test_codex_question_navigation_and_cancel_twice(native_question):
    state, request, _ = native_question
    initial = request().body
    assert asyncio.run(srv.api_prompt(request("next"))).status == 200
    assert asyncio.run(srv.api_prompt(request("previous"))).status == 200
    assert request().body["prompt_id"] == initial["prompt_id"]
    state.update(notes=True, text="draft")
    assert asyncio.run(srv.api_prompt(request("cancel"))).status == 200
    assert state["sent"] == ["\x0e", "\x10", "\x03", "\x03"]


@pytest.mark.parametrize("extra,status", [
    ({"prompt_id": "stale"}, 409), ({"text": "bad\x1b[31m"}, 400),
    ({"text": "bad\x03"}, 400), ({"option_index": -1}, 400), ({"option_index": True}, 400),
])
def test_codex_question_invalid_no_typing(native_question, extra, status):
    state, request, _ = native_question
    req = request()
    req.body.update(extra)
    assert asyncio.run(srv.api_prompt(req)).status == status
    assert state["sent"] == []


def test_codex_question_wrong_provider_busy_and_unchanged(native_question):
    state, request, pane = native_question
    req = request()
    srv.KNOWN_AGENTS["PANE"] = "claude"
    assert asyncio.run(srv.api_prompt(req)).status == 403
    srv.KNOWN_AGENTS["PANE"] = "codex"
    srv._PANE_WRITES.add("PANE")
    assert asyncio.run(srv.api_prompt(req)).status == 409
    srv._PANE_WRITES.clear()
    state["stuck"] = True
    assert asyncio.run(srv.api_prompt(req)).status == 409
    assert state["sent"] == ["\r"]


def test_codex_send_blocks_question_and_uses_bracketed_paste(native_question):
    state, _, _ = native_question
    req = CommandRequest({"uuid": "pane", "text": "hello\nworld"})
    assert asyncio.run(srv.api_send(req)).status == 409
    assert state["sent"] == []
    state["done"] = True
    assert asyncio.run(srv.api_send(req)).status == 200
    assert state["sent"] == ["\x1b[200~hello\nworld\x1b[201~", "\r"]


def test_codex_approval_choose_uses_arrows_and_enter(command_api, monkeypatch):
    state = {"screen": CODEX_APPROVAL_SCREEN, "sent": []}

    async def send(value):
        state["sent"].append(value)
        if value == "\x1b[B":
            state["screen"] = state["screen"].replace("› 1.", "  1.").replace("  2.", "› 2.")
        elif value == "\r":
            state["screen"] = CODEX_IDLE

    pane = AsyncMock()
    pane.async_get_variable.return_value = "codex"
    pane.async_send_text.side_effect = send
    monkeypatch.setattr(srv, "all_sessions", AsyncMock(return_value={"PANE": pane}))
    monkeypatch.setattr(srv, "pane_text", AsyncMock(side_effect=lambda _: state["screen"]))
    prompt = srv.detect_prompt(state["screen"], "codex", "PANE")
    req = CommandRequest({"uuid": "pane", "prompt_id": prompt["id"], "action": "choose", "option_index": 1})
    response = asyncio.run(srv.api_prompt(req))
    assert response.status == 200
    assert json.loads(response.text)["prompt"] is None
    assert state["sent"] == ["\x1b[B", "\r"]


def test_codex_freeform_replaces_existing_draft(command_api, monkeypatch):
    state = {"draft": "old draft", "done": False, "sent": []}

    def screen():
        if state["done"]:
            return CODEX_IDLE
        return ("Question 1/1 (1 unanswered)\nShare details.\n› "
                + (state["draft"] or "Type your answer (optional)")
                + "\nenter to submit answer | esc to interrupt")

    async def send(value):
        state["sent"].append(value)
        if value == "\x03":
            state["draft"] = ""
        elif value.startswith("\x1b[200~"):
            state["draft"] = value[6:-6]
        elif value == "\r":
            state["done"] = True

    pane = AsyncMock()
    pane.async_get_variable.return_value = "codex"
    pane.async_send_text.side_effect = send
    monkeypatch.setattr(srv, "all_sessions", AsyncMock(return_value={"PANE": pane}))
    monkeypatch.setattr(srv, "pane_text", AsyncMock(side_effect=lambda _: screen()))
    prompt = srv.detect_prompt(screen(), "codex", "PANE")
    req = CommandRequest({"uuid": "pane", "prompt_id": prompt["id"], "action": "submit", "text": "new details"})
    assert asyncio.run(srv.api_prompt(req)).status == 200
    assert state["sent"] == ["\x03", "\x1b[200~new details\x1b[201~", "\r"]


def test_detect_prompt_question_len_capped():
    long_q = "Why " * 80 + "?"
    text = f"{long_q}\n❯ 1. Yes\n  2. No\n"
    out = srv.detect_prompt(text)
    assert out is not None
    assert len(out["question"]) <= 160


GROK_IDLE = (
    "  ╭──────────────────────────────────────────────────────────────────────────╮\n"
    "  │ ❯                                                                        │\n"
    "  ╰─────────────────────────────────────── Grok 4.6 (high) · always-approve ─╯\n"
)

GROK_WORKING = (
    "  ❯ fix the picker overlay\n"
    "  ◆ Thinking...\n"
    "  ◈ read_file server.py\n"
    "  ╭──────────────────────────────────────────────────────────────────────────╮\n"
    "  │ ❯                                                                        │\n"
    "  ╰─────────────────────────────────────── Grok 4.6 (high) · always-approve ─╯\n"
)

GROK_DONE = (
    "  ❯ fix the picker overlay\n"
    "  ◆ Thinking...\n"
    "  Worked for 12s\n"
    "  ╭──────────────────────────────────────────────────────────────────────────╮\n"
    "  │ ❯                                                                        │\n"
    "  ╰─────────────────────────────────────── Grok 4.6 (high) · always-approve ─╯\n"
)

GROK_WAITING_BG = (
    "  ◎ 1 command still running · send a message to interrupt\n"
    + GROK_IDLE
)

GROK_STATUS_WORKING = (
    "  ◆ Thought for 1.4s\n"
    "  ◈ Running 3 subagents, Reading 6 files\n"
    "   ⠹ Preparing MCP tool… 0.4s                              4m5s ⇣146k [stop]\n"
    + GROK_IDLE
)

GROK_IDLE_LEFTOVER_TOOLS = (
    "  ◆ Thought for 1.4s\n"
    "  ◈ Running 3 subagents, Reading 6 files\n"
    "  ◈ read_file server.py\n"
    + GROK_IDLE
)

GROK_COMPACT_WORKING = (
    "   #1 fix the picker overlay : aiThe overlay is drawn … (+27 lines)\n"
    + GROK_IDLE
)

GROK_SLASH_MENU = (
    "  ──────────────────────────────────────────────────────────────────────────1─\n"
    "    ❯ /model Switch the active model\n"
    "  ────────────────────────────────────────────────────────────────────────────\n"
    "  ╭──────────────────────────────────────────────────────────────────────────╮\n"
    "  │ ❯ /model                                                                 │\n"
    "  ╰─────────────────────────────────────── Grok 4.6 (high) · always-approve ─╯\n"
)

GROK_MODEL_PICKER = (
    "  ──────────────────────────────────────────────────────────────────────────2─\n"
    "    ❯ Grok 4.6 (current) SpaceXAI's latest frontier model\n"
    "      Grok 4.5\n"
    "  ────────────────────────────────────────────────────────────────────────────\n"
    "  ╭──────────────────────────────────────────────────────────────────────────╮\n"
    "  │ ❯ /model <model> [effort]                                                │\n"
    "  ╰─────────────────────────────────────── Grok 4.6 (high) · always-approve ─╯\n"
)

GROK_EFFORT_PICKER = (
    "  ──────────────────────────────────────────────────────────────────────────4─\n"
    "    ❯ Extra High Effort    Highest effort and reasoning level\n"
    "      High Effort (active) Higher implementation quality with extensive\n"
    "                            reasoning\n"
    "      Medium Effort        Balanced effort with standard implementation and\n"
    "                            testing\n"
    "      Low Effort           Quick, fast implementations\n"
    "  ────────────────────────────────────────────────────────────────────────────\n"
    "  ╭──────────────────────────────────────────────────────────────────────────╮\n"
    "  │ ❯ /effort <level>                                                        │\n"
    "  ╰─────────────────────────────────────── Grok 4.6 (high) · always-approve ─╯\n"
)


def test_detect_prompt_grok_idle_is_none():
    assert srv.detect_prompt(GROK_IDLE, "grok") is None
    assert srv.detect_prompt(GROK_SLASH_MENU, "grok") is None


GROK_NARROW_MODEL_PICKER = (
    "  ──────────────────────────────────────────────2─\n"
    "    ❯ Grok 4.6 (current) SpaceXAI's latest\n"
    "                          frontier model\n"
    "      Grok 4.5\n"
    "  ────────────────────────────────────────────────\n"
    "  ╭──────────────────────────────────────────────╮\n"
    "  │ ❯ /model <model> [effort]                    │\n"
    "  ╰─────────── Grok 4.6 (high) · always-approve ─╯\n"
)


def test_detect_prompt_grok_model_picker_narrow_pane():
    out = srv.detect_prompt(GROK_NARROW_MODEL_PICKER, "grok")
    assert [o["label"] for o in out["options"]] == ["Grok 4.6 (current)", "Grok 4.5"]
    assert out["options"][0]["desc"] == "SpaceXAI's latest frontier model"


def test_grok_strips_model_icon_from_label():
    text = (
        "  ──────────────────────────────────────────────2─\n"
        "    ❯ 🖼 Grok 4.6 (current) SpaceXAI's latest\n"
        "      Grok 4.5\n"
        "  ────────────────────────────────────────────────\n"
        "  ╭──────────────────────────────────────────────╮\n"
        "  │ ❯ /model <model> [effort]                    │\n"
        "  ╰─────────── Grok 4.6 (high) · always-approve ─╯\n"
    )
    out = srv.detect_prompt(text, "grok")
    assert out["options"][0]["label"] == "Grok 4.6 (current)"
    assert out["options"][1]["label"] == "Grok 4.5"


def test_detect_prompt_grok_model_picker():
    out = srv.detect_prompt(GROK_MODEL_PICKER, "grok")
    assert out["provider"] == "grok"
    assert out["kind"] == "picker"
    assert out["variant"] == "model"
    assert [o["label"] for o in out["options"]] == ["Grok 4.6 (current)", "Grok 4.5"]
    assert out["options"][0]["selected"]
    assert not out["options"][1]["selected"]
    assert out["options"][0]["desc"] == "SpaceXAI's latest frontier model"
    assert out["actions"] == ["choose", "cancel"]
    assert srv.detect_prompt(GROK_MODEL_PICKER) is None


def test_detect_prompt_grok_effort_picker_wraps_description():
    out = srv.detect_prompt(GROK_EFFORT_PICKER, "grok")
    assert out["variant"] == "effort"
    assert [o["label"] for o in out["options"]] == [
        "Extra High Effort", "High Effort (active)", "Medium Effort", "Low Effort"]
    assert out["options"][1]["desc"] == (
        "Higher implementation quality with extensive reasoning")
    assert out["options"][2]["desc"] == (
        "Balanced effort with standard implementation and testing")
    assert out["options"][0]["selected"]
    assert not out["options"][1]["selected"]


def test_grok_idle_screen_is_not_running():
    assert not srv._grok_busy(GROK_IDLE)
    assert srv._grok_idle(GROK_IDLE)
    assert srv.detect_running("grok", GROK_IDLE) is False


def test_grok_busy_matches_thinking_without_worked_for():
    assert srv._grok_busy(GROK_WORKING)
    assert not srv._grok_idle(GROK_WORKING)
    assert srv.detect_running("grok", GROK_WORKING) is True


def test_grok_busy_clears_after_worked_for():
    assert not srv._grok_busy(GROK_DONE)
    assert srv.detect_running("grok", GROK_DONE) is False


def test_grok_busy_matches_still_running_line():
    assert srv._grok_busy(GROK_WAITING_BG)
    assert srv.detect_running("grok", GROK_WAITING_BG) is True


def test_grok_status_line_stop_is_running():
    assert srv._grok_busy(GROK_STATUS_WORKING)
    assert srv.detect_running("grok", GROK_STATUS_WORKING) is True


def test_grok_leftover_tools_are_not_running():
    assert not srv._grok_busy(GROK_IDLE_LEFTOVER_TOOLS)
    assert srv._grok_idle(GROK_IDLE_LEFTOVER_TOOLS)
    assert srv.detect_running("grok", GROK_IDLE_LEFTOVER_TOOLS) is False


def test_grok_compact_prompt_is_running():
    assert srv._grok_busy(GROK_COMPACT_WORKING)
    assert srv.detect_running("grok", GROK_COMPACT_WORKING) is True


def test_grok_journal_open_turn(tmp_path):
    hist = tmp_path / "chat_history.jsonl"
    ev = tmp_path / "events.jsonl"
    hist.write_text("")
    ev.write_text(json.dumps({"type": "turn_started", "ts": "t0"}) + "\n")
    # Idle composer without a status line is stopped, even if the journal
    # missed turn_ended and still says the turn is open.
    assert srv.detect_running("grok", GROK_IDLE, str(hist)) is False
    assert srv.detect_running("grok", GROK_STATUS_WORKING, str(hist)) is True
    ev.write_text(ev.read_text() + json.dumps({"type": "turn_ended"}) + "\n")
    assert srv.detect_running("grok", GROK_IDLE, str(hist)) is False


def test_grok_idle_screen_beats_stale_journal(tmp_path):
    hist = tmp_path / "chat_history.jsonl"
    ev = tmp_path / "events.jsonl"
    hist.write_text("")
    ev.write_text(json.dumps({"type": "turn_started"}) + "\n")
    assert srv.detect_running("grok", GROK_IDLE_LEFTOVER_TOOLS, str(hist)) is False


def test_grok_screen_busy_beats_ended_journal(tmp_path):
    hist = tmp_path / "chat_history.jsonl"
    ev = tmp_path / "events.jsonl"
    hist.write_text("")
    ev.write_text(json.dumps({"type": "turn_started"}) + "\n"
                  + json.dumps({"type": "turn_ended"}) + "\n")
    assert srv.detect_running("grok", GROK_WORKING, str(hist)) is True


def test_detect_running_claude_has_no_overlay():
    assert srv.detect_running("claude", STATUS_LINE_AUTO) is None


def test_detect_grok_status_footer():
    assert srv.detect_grok_status(GROK_IDLE) == {
        "model": "Grok 4.6", "effort": "high", "mode": "always-approve"}


def test_grok_prompt_episode_id_stable(monkeypatch):
    first = srv.detect_prompt(GROK_MODEL_PICKER, "grok", "PANE")
    second = srv.detect_prompt(GROK_MODEL_PICKER, "grok", "PANE")
    assert first["id"] == second["id"]
    assert srv.detect_prompt(GROK_IDLE, "grok", "PANE") is None
    again = srv.detect_prompt(GROK_MODEL_PICKER, "grok", "PANE")
    assert again["id"] != first["id"]


def test_grok_models_from_cache(tmp_path, monkeypatch):
    cache = tmp_path / "models_cache.json"
    cache.write_text(json.dumps({"models": {
        "grok-4.6": {"info": {"id": "grok-4.6", "name": "Grok 4.6", "hidden": False,
                              "reasoning_efforts": [
                                  {"id": "xhigh", "default": False},
                                  {"id": "high", "default": True}]}},
        "hidden": {"info": {"id": "hidden", "hidden": True}},
    }}))
    monkeypatch.setattr(srv, "GROK_MODELS_CACHE", str(cache))
    srv._GROK_MODELS.update(at=0, models=[])
    models = asyncio.run(srv._grok_models())
    assert models == [{"id": "grok-4.6", "label": "Grok 4.6",
                       "efforts": ["xhigh", "high"], "default_effort": "high"}]


def test_grok_match_model_accepts_display_name():
    catalog = {"grok-4.6": {"id": "grok-4.6", "label": "Grok 4.6"}}
    assert srv._grok_match_model(catalog, "Grok 4.6")["id"] == "grok-4.6"
    assert srv._grok_match_model(catalog, "grok-4.6")["id"] == "grok-4.6"
    assert srv._grok_match_model(catalog, "nope") is None


def test_grok_open_model_picker(command_api, monkeypatch):
    state = {"screen": GROK_IDLE, "sent": []}

    async def send(value):
        state["sent"].append(value)
        if value == "/model":
            state["screen"] = GROK_SLASH_MENU
        elif value == "\r":
            state["screen"] = GROK_MODEL_PICKER

    pane = AsyncMock()
    pane.async_get_variable.return_value = "grok"
    pane.async_send_text.side_effect = send
    monkeypatch.setattr(srv, "all_sessions", AsyncMock(return_value={"PANE": pane}))
    monkeypatch.setattr(srv, "pane_text", AsyncMock(side_effect=lambda _: state["screen"]))
    monkeypatch.setattr(srv, "KNOWN_AGENTS", {"PANE": "grok"})
    response = asyncio.run(srv.api_model(CommandRequest({"uuid": "pane"})))
    assert response.status == 200
    body = json.loads(response.text)
    assert body["prompt"]["variant"] == "model"
    assert [o["label"] for o in body["prompt"]["options"]] == [
        "Grok 4.6 (current)", "Grok 4.5"]
    assert state["sent"][:3] == ["\x15", "/model", "\r"]


def test_grok_open_effort_picker(command_api, monkeypatch):
    state = {"screen": GROK_IDLE, "sent": []}

    async def send(value):
        state["sent"].append(value)
        if value == "\r":
            state["screen"] = GROK_EFFORT_PICKER

    pane = AsyncMock()
    pane.async_get_variable.return_value = "grok"
    pane.async_send_text.side_effect = send
    monkeypatch.setattr(srv, "all_sessions", AsyncMock(return_value={"PANE": pane}))
    monkeypatch.setattr(srv, "pane_text", AsyncMock(side_effect=lambda _: state["screen"]))
    monkeypatch.setattr(srv, "KNOWN_AGENTS", {"PANE": "grok"})
    response = asyncio.run(srv.api_effort(CommandRequest({"uuid": "pane"})))
    assert response.status == 200
    body = json.loads(response.text)
    assert body["prompt"]["variant"] == "effort"
    assert len(body["prompt"]["options"]) == 4


def test_grok_choose_option_then_effort(command_api, monkeypatch):
    state = {"screen": GROK_MODEL_PICKER, "sent": []}

    async def send(value):
        state["sent"].append(value)
        if value == "\x1b[B":
            state["screen"] = GROK_MODEL_PICKER.replace("    ❯ Grok 4.6", "      Grok 4.6").replace(
                "      Grok 4.5", "    ❯ Grok 4.5")
        elif value == "\r":
            state["screen"] = GROK_EFFORT_PICKER

    pane = AsyncMock()
    pane.async_get_variable.return_value = "grok"
    pane.async_send_text.side_effect = send
    monkeypatch.setattr(srv, "all_sessions", AsyncMock(return_value={"PANE": pane}))
    monkeypatch.setattr(srv, "pane_text", AsyncMock(side_effect=lambda _: state["screen"]))
    monkeypatch.setattr(srv, "KNOWN_AGENTS", {"PANE": "grok"})
    prompt = srv.detect_prompt(state["screen"], "grok", "PANE")
    req = CommandRequest({"uuid": "pane", "prompt_id": prompt["id"],
                          "action": "choose", "option_index": 1})
    response = asyncio.run(srv.api_prompt(req))
    assert response.status == 200
    body = json.loads(response.text)
    assert body["prompt"]["variant"] == "effort"
    assert state["sent"] == ["\x1b[B", "\r"]


def test_grok_cancel_picker(command_api, monkeypatch):
    state = {"screen": GROK_MODEL_PICKER}

    async def send(value):
        if value == "\x1b":
            state["screen"] = GROK_IDLE

    pane = AsyncMock()
    pane.async_get_variable.return_value = "grok"
    pane.async_send_text.side_effect = send
    monkeypatch.setattr(srv, "all_sessions", AsyncMock(return_value={"PANE": pane}))
    monkeypatch.setattr(srv, "pane_text", AsyncMock(side_effect=lambda _: state["screen"]))
    monkeypatch.setattr(srv, "KNOWN_AGENTS", {"PANE": "grok"})
    prompt = srv.detect_prompt(GROK_MODEL_PICKER, "grok", "PANE")
    req = CommandRequest({"uuid": "pane", "prompt_id": prompt["id"], "action": "cancel"})
    response = asyncio.run(srv.api_prompt(req))
    assert response.status == 200
    assert json.loads(response.text)["prompt"] is None


def test_grok_slash_model_sends_one_enter(command_api, monkeypatch):
    pane = AsyncMock()
    pane.async_get_variable.return_value = "grok"
    monkeypatch.setattr(srv, "all_sessions", AsyncMock(return_value={"PANE": pane}))
    monkeypatch.setattr(srv, "pane_text", AsyncMock(return_value=GROK_IDLE))
    monkeypatch.setattr(srv, "KNOWN_AGENTS", {"PANE": "grok"})
    monkeypatch.setattr(srv, "_grok_models", AsyncMock(return_value=[
        {"id": "grok-4.6", "label": "Grok 4.6", "efforts": ["high"], "default_effort": "high"}]))
    monkeypatch.setattr(srv, "_read_grok_default", lambda: "grok-4.6")
    monkeypatch.setattr(srv.asyncio, "ensure_future",
                        lambda coro: coro.close() if hasattr(coro, "close") else None)
    response = asyncio.run(srv.api_model(CommandRequest({"uuid": "pane", "model": "grok-4.6"})))
    assert response.status == 200
    assert [c.args[0] for c in pane.async_send_text.await_args_list] == [
        "\x15", "/model grok-4.6", "\r"]


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


def test_transcript_digest_grok_user_query(tmp_path):
    p = tmp_path / "chat_history.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "system", "content": "You are Grok"}),
        json.dumps({"type": "user", "content": [
            {"type": "text", "text": "<user_info>os</user_info>"}]}),
        json.dumps({"type": "user", "content": [
            {"type": "text", "text": "<system-reminder>skills</system-reminder>"}]}),
        json.dumps({"type": "user", "content": [
            {"type": "text", "text": "<user_query>\nFix the picker\n</user_query>"}]}),
        json.dumps({"type": "assistant", "content": [
            {"type": "text", "text": "I'll fix the picker overlay."}]}),
        json.dumps({"type": "assistant", "content": []}),
    ]) + "\n")
    d = srv._transcript_digest(p)
    assert "Fix the picker" in d
    assert "I'll fix the picker overlay." in d
    assert "You are Grok" not in d
    assert "skills" not in d


def test_transcript_digest_codex_payload(tmp_path):
    p = tmp_path / "rollout.jsonl"
    p.write_text("\n".join([
        json.dumps({"payload": {"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": "AGENTS.md"}]}}),
        json.dumps({"payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "switch the model"}]}}),
        json.dumps({"payload": {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "Opening /model now."}]}}),
        json.dumps({"payload": {"type": "agent_message", "content": [
            {"type": "input_text", "text": "subagent noise"}]}}),
    ]) + "\n")
    d = srv._transcript_digest(p)
    assert "switch the model" in d
    assert "Opening /model now." in d
    assert "AGENTS.md" not in d
    assert "subagent noise" not in d


def test_codex_ops_retains_latest_turn_context_header(tmp_path):
    p = tmp_path / "rollout.jsonl"
    contexts = [
        {"type": "turn_context", "model": "gpt-5.6-terra", "effort": "high",
         "approval_policy": "on-request",
         "sandbox_policy": {"type": "workspace-write"},
         "permission_profile": {"type": "managed"}},
        {"type": "turn_context", "model": "gpt-6-astra", "effort": "xhigh",
         "approval_policy": "never",
         "sandbox_policy": {"type": "danger-full-access"},
         "permission_profile": {"type": "disabled"}},
    ]
    p.write_text(json.dumps({"type": "turn_context", "payload": contexts[0]}) + "\n")
    srv._NATIVE.pop(str(p), None)
    first = srv.codex_ops(str(p))
    assert (first["model"], first["effort"], first["mode"]) == (
        "gpt-5.6-terra", "high", "Default")

    with p.open("a") as fh:
        fh.write(json.dumps({"type": "turn_context", "payload": contexts[1]}) + "\n")
    latest = srv.codex_ops(str(p))
    assert (latest["model"], latest["effort"], latest["mode"]) == (
        "gpt-6-astra", "xhigh", "Full access")


@pytest.mark.parametrize("total,expected", [
    (0, 0), (129_200, 50), (258_400, 100), (300_000, 100),
])
def test_codex_ops_context_percentage_is_clamped(tmp_path, total, expected):
    p = tmp_path / "rollout.jsonl"
    p.write_text(json.dumps({"type": "event_msg", "payload": {
        "type": "token_count", "info": {
            "last_token_usage": {"total_tokens": total},
            "model_context_window": 258_400}}}) + "\n")
    srv._NATIVE.pop(str(p), None)

    assert srv.codex_ops(str(p))["ctx"] == expected


def test_codex_ops_invalid_context_followup_preserves_last_valid(tmp_path):
    p = tmp_path / "rollout.jsonl"
    p.write_text(json.dumps({"type": "event_msg", "payload": {
        "type": "token_count", "info": {
            "last_token_usage": {"total_tokens": 129_200},
            "model_context_window": 258_400}}}) + "\n")
    srv._NATIVE.pop(str(p), None)
    assert srv.codex_ops(str(p))["ctx"] == 50

    with p.open("a") as fh:
        for info in (
            {"last_token_usage": None, "model_context_window": 258_400},
            {"last_token_usage": {"total_tokens": "invalid"},
             "model_context_window": 258_400},
            {"last_token_usage": {"total_tokens": 2},
             "model_context_window": 0},
        ):
            fh.write(json.dumps({"type": "event_msg", "payload": {
                "type": "token_count", "info": info}}) + "\n")

    assert srv.codex_ops(str(p))["ctx"] == 50


def test_codex_fleet_uses_turn_context_header_fallbacks(tmp_path, monkeypatch):
    journal = tmp_path / "rollout.jsonl"
    journal.write_text(
        json.dumps({"type": "turn_context", "payload": {
            "type": "turn_context", "model": "gpt-5.6-luna", "effort": "medium",
            "approval_policy": "never", "sandbox_policy": {"type": "read-only"},
            "permission_profile": {"type": "managed"}}}) + "\n" +
        json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "last_token_usage": {"total_tokens": 129_200},
                "model_context_window": 258_400}}}) + "\n")
    pane = tmp_path / "pane.json"
    hook = {
        "fleet_key": "pane", "provider": "codex", "session_id": "session",
        "iterm_pane": "w0t0p0:PANE",
        "workspace": {"current_dir": str(tmp_path)}}
    pane.write_text(json.dumps(hook))
    monkeypatch.setattr(srv, "FLEET_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "journal_path", lambda provider, sid: str(journal))
    srv._NATIVE.pop(str(journal), None)

    row = next(iter(srv.read_fleet_files().values()))
    assert (row["model"], row["effort"], row["mode"]) == (
        "gpt-5.6-luna", "medium", "Read only")
    assert row["ctx"] == 50

    hook["context_window"] = {"used_percentage": 23}
    pane.write_text(json.dumps(hook))
    assert next(iter(srv.read_fleet_files().values()))["ctx"] == 23


def test_transcript_digest_claude_message(tmp_path):
    p = tmp_path / "claude.jsonl"
    p.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "fix permissions"}]}}) + "\n"
        + json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Working on it."}]}}) + "\n")
    d = srv._transcript_digest(p)
    assert "fix permissions" in d
    assert "Working on it." in d


def test_prompted_spawn_honors_configured_provider_and_bounds_prompt():
    for selected in srv.PROVIDERS:
        provider, prompt = srv._spawn_options({
            "provider": selected, "initial_prompt": "  inspect safely  "})
        assert provider == selected
        assert prompt == "inspect safely"

    try:
        srv._spawn_options({"initial_prompt": "x" * (srv.INITIAL_PROMPT_MAX + 1)})
    except ValueError as exc:
        assert "exceeds" in str(exc)
    else:
        raise AssertionError("oversized initial prompt was accepted")


def test_prompted_spawn_rejects_invalid_provider_via_api(command_api, monkeypatch):
    monkeypatch.setattr(srv, "APP", AsyncMock())
    response = asyncio.run(srv.api_spawn(CommandRequest({
        "provider": "invalid", "initial_prompt": "inspect safely"})))
    assert response.status == 400
    assert "unknown agent" in response.text


def test_initial_prompt_waits_for_agent_ui_before_delivery(monkeypatch):
    pane = AsyncMock()
    pane.session_id = "pane"
    screens = AsyncMock(side_effect=["launching…", "shift+tab to cycle"])
    monkeypatch.setattr(srv, "pane_text", screens)

    async def no_sleep(_):
        pass

    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)
    delivered = asyncio.run(srv._deliver_initial_prompt(
        pane, "diagnose safely", timeout=2))

    assert delivered is True
    assert [call.args[0] for call in pane.async_send_text.await_args_list] == [
        "diagnose safely", "\r"]
    assert screens.await_count == 2


def test_codex_initial_prompt_uses_bracketed_paste(monkeypatch):
    pane = AsyncMock()
    pane.session_id = "pane"
    monkeypatch.setattr(srv, "pane_text", AsyncMock(
        return_value="Ask Codex to do anything"))

    async def no_sleep(_):
        pass

    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)
    assert asyncio.run(srv._deliver_initial_prompt(
        pane, "diagnose safely", provider="codex", timeout=2)) is True
    assert [call.args[0] for call in pane.async_send_text.await_args_list] == [
        "\x1b[200~diagnose safely\x1b[201~", "\r"]


def test_system_metric_actions_use_prompted_spawn_and_pending_pane():
    source = (srv.HERE / "static" / "index.html").read_text()
    assert source.count("${sysActionButton('") == 4
    assert "post('/api/spawn', {provider: SPAWN_AGENT, initial_prompt: SYS_ACTION_PROMPTS[kind]})" in source
    assert "PENDING_PANE = r.uuid" in source
    for phrase in ("energy drain", "Mac’s temperature", "CPU use", "free disk space"):
        assert phrase in source


def test_claude_dividers_use_nowrap_fitline_without_matching_prose():
    source = (srv.HERE / "static" / "index.html").read_text()
    assert "(p === 'claude' && /^\\s*[─━]{4,}\\s*$/.test(l))" in source


def test_context_rule_is_composer_top_edge():
    source = (srv.HERE / "static" / "index.html").read_text()
    cbox = source.index('<div class="cbox">')
    composer = source.index('<div class="composer">')
    ctxrow = source.index('<div class="ctxrow">', cbox)
    context = source.index('<div class="ctxrule" id="ctxrule"', composer)
    clear = source.index('id="clearctx"', cbox)
    textarea = source.index('<textarea id="input"', cbox)
    assert composer < cbox < ctxrow < context < clear < textarea
    assert source.count('id="ctxrule"') == 1
    assert 'aria-label="clear context"' in source
    assert ".ctxrow{display:flex;align-items:center;gap:8px;margin:0 4px 6px}" in source
    assert ".ctxrule{flex:1;min-width:0;height:5px;width:auto;margin:0;border-radius:999px" in source
    assert "background:var(--panel3);box-shadow:inset 0 0 0 1px" in source
    assert "border-radius:inherit;transition:width .35s ease" in source
    fn = source[source.index("function clearContext()"):source.index("async function runCmd")]
    assert "if (!current) return" in fn
    assert "runCmd('clear')" in fn
    chrome = source[source.index("function applyProviderChrome()"):source.index("function updateMeta()")]
    assert "clear.disabled = false" in chrome
    assert "clear.hidden = false" in chrome
    assert "clear.hidden = off" not in chrome
    assert "const off = provider === 'codex'" not in chrome


def test_fleet_refresh_repaints_open_session_context():
    source = (srv.HERE / "static" / "index.html").read_text()
    render_start = source.index("function renderFleet(rows){")
    current_start = source.index("if (current){", render_start)
    current_refresh = source[current_start:source.index("const panes", current_start)]

    assert current_refresh.index("current = fresh") < current_refresh.index(
        "paintCtxBar(fresh.ctx)")


def test_session_keeps_global_geometry_and_mobile_composer_lift():
    source = (srv.HERE / "static" / "index.html").read_text()
    assert "html,body{margin:0;height:100%;overscroll-behavior:none;" in source
    assert "100dvh" not in source
    assert "@media (max-width:700px){\n    #s-session{height:100%;min-height:0}" in source
    assert "    .composer{padding-bottom:17px}" in source
    assert "calc(env(safe-area-inset-bottom) + 17px)" not in source


def test_dashboard_navigation_uses_material_symbols_and_flat_chrome():
    source = (srv.HERE / "static" / "index.html").read_text()
    assert '#s-session > header{position:relative;z-index:3;background:var(--bg);border-bottom:0;' in source
    assert "box-shadow:0 16px 36px -16px rgba(0,0,0,.55);" not in source
    assert "-webkit-mask-image:linear-gradient(to bottom,rgba(0,0,0,.08) 0,rgba(0,0,0,.42) 38px,#000 104px);" in source
    assert "mask-image:linear-gradient(to bottom,rgba(0,0,0,.08) 0,rgba(0,0,0,.42) 38px,#000 104px)" in source
    assert '#s-session > header{background:var(--bg);border-bottom:1px solid var(--line);' not in source
    assert source.count('<div class="tabbar">') == 5
    for label in ("yard", "history", "usage", "system"):
        assert source.count(f'<span class="tab-label">{label}</span>') == 5
    for label in ("yard", "history", "usage", "system"):
        assert source.count(f'aria-label="{label}"') == 5
    assert "Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20,300..700,0,-25" in source
    assert (
        "icon_names=attach_file,delete_sweep,history,home,mic,notifications,"
        "photo_camera,settings,stop,tune&amp;display=block"
    ) in source
    for icon in ("home", "history", "tune", "settings"):
        assert source.count(
            f'<span class="material-symbols-rounded" aria-hidden="true">{icon}</span>'
        ) == 5
    assert "query_stats" not in source
    assert '<span class="material-symbols-rounded" aria-hidden="true">delete_sweep</span>' in source
    assert '<span class="material-symbols-rounded" aria-hidden="true">notifications</span>' in source
    assert '<span class="material-symbols-rounded" aria-hidden="true">attach_file</span>' in source
    assert '<span class="material-symbols-rounded" aria-hidden="true">photo_camera</span>' in source
    assert '<span class="material-symbols-rounded i-mic" aria-hidden="true">mic</span>' in source
    assert '<span class="material-symbols-rounded i-stop" aria-hidden="true">stop</span>' in source
    assert '<span class="material-symbols-rounded" aria-hidden="true">send</span>' not in source
    assert '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M3.5 20.6l16.9-8.1c.8-.4.8-1.6 0-2L3.5 2.4c-.8-.4-1.6.4-1.3 1.2L4 11l-1.8 6.4c-.3.8.5 1.6 1.3 1.2z"/></svg>' in source
    assert ".send svg{width:20px;height:20px;display:block}" in source
    assert "#micbtn{display:none}" in source
    assert '#attachbtn .material-symbols-rounded,#camerabtn .material-symbols-rounded{' in source
    assert 'font-size:21px;font-variation-settings:"FILL" 0,"wght" 700,"GRAD" -25,"opsz" 21}' in source
    assert "#sendbtn{width:40px;height:40px}" in source
    assert '<div class="statusbar">' not in source
    assert ".statusbar{" not in source
    assert ".tabbar{display:flex;flex:none;border-top:1px solid var(--line);background:var(--bg);" in source
    assert '.tabbar button .material-symbols-rounded{font-size:21px;' in source
    assert 'font-variation-settings:"FILL" 0,"wght" 600,"GRAD" -25,"opsz" 21}' in source
    assert '.tabbar button .tab-label{font:600 10px var(--ui);' in source
    assert 'letter-spacing:.06em;margin-left:5px}' in source
    assert ".tabbar button.cur{color:var(--blue)}" in source
    assert "border-top-color:var(--blue)" not in source
    assert "box-shadow:0 0 0 2px var(--bg)" in source
    assert ".sys-grid{display:grid;grid-template-columns:1fr 1fr;gap:0;" in source
    assert ".sys-cell{min-width:0;padding:16px 14px;background:none;border:0;" in source
    assert '<span class="sys-unit">°C</span>' in source
    assert "<s>°C</s>" not in source


def test_grok_prompt_is_hoisted_above_faded_pane():
    source = (srv.HERE / "static" / "index.html").read_text()
    assert 'id="grok-q"' not in source
    assert "#grok-q,.grok-q" not in source
    assert ".grok-q-time" not in source
    assert ".grok-q-text" not in source
    assert ".pane .ln.grok-p" not in source
    assert ".pane.theme-grok{" in source
    theme = source[source.index(".pane.theme-grok{"):
                   source.index("}", source.index(".pane.theme-grok{")) + 1]
    assert "mask-image:none" not in theme
    assert "--chat-add:" in theme
    assert "function grokPromptEnd(" not in source
    assert "function grokPeelStamp(" not in source


def test_grok_transcript_keeps_history_like_codex():
    source = (srv.HERE / "static" / "index.html").read_text()
    connect = source[source.index("function connectPane(uuid){"):
                     source.index("function pushNav(state){")]
    assert "if (d.history) history = d.history;" in connect
    assert "if (d.history_add) history.push(...d.history_add);" in connect
    assert "current.provider !== 'grok'" not in connect
    assert "d.grok_prompt" not in connect
    paint = source[source.index("function paint(force){"):source.index("const PIN_SLACK")]
    assert "history.join('\\n')" in paint
    assert "grokLastPrompt" not in paint
    assert "grokTidyLines" not in paint
    assert "grokReflow" not in paint
    assert "grok-p" not in paint
    assert "grokTurn" not in paint
    assert "el.className = 'ln' + lineRenderClass(rawLines[i], prov);" in paint


def test_grok_working_status_paints_above_composer():
    source = (srv.HERE / "static" / "index.html").read_text()
    assert 'id="grok-status"' not in source
    assert ".grok-status{" not in source
    assert ".composer.grok-on" not in source
    assert "function paintGrokStatus(" not in source
    assert "function grokCollectStatus(" not in source
    assert "function grokResolvePrompt(" not in source
    assert "function grokStripHoisted(" not in source
    assert "current.grok_prompt" not in source
    connect = source[source.index("function connectPane(uuid){"):
                     source.index("function pushNav(state){")]
    assert "d.grok_prompt" not in connect


def test_grok_ws_uses_iterm_history_like_codex():
    source = pathlib.Path(srv.__file__).read_text()
    ws = source[source.index("async def ws_pane"):source.index("async def ws_fleet")]
    assert "grok_history_thin" not in ws
    assert "grok_journal_turns" not in ws
    assert "grok_from_journal" not in ws
    assert "grok_prompt" not in ws
    assert "if more and not grok_from_journal:" not in ws
    assert "if more:" in ws
    assert '"history_add": more' in ws


def test_page_swipe_removed_and_usage_swipe_scoped_to_usage_body():
    source = (srv.HERE / "static" / "index.html").read_text()
    assert 'function pageSwipe' not in source
    assert '(function pageSwipe()' not in source
    assert '(function usageSwipe()' in source
    assert "e.target.closest('#usage-body')" in source
    assert "setUsageProvider(next)" in source
    assert "Math.abs(dx) < 72" in source


def test_dir_picker_paints_recent_section():
    source = (srv.HERE / "static" / "index.html").read_text()
    start = source.index("function renderDirPicker(data){")
    fn = source[start:source.index("function filterDirRows(", start)]
    assert "data.recents" in fn
    assert "addLab('recent')" in fn
    assert ".dp-lab{" in source
