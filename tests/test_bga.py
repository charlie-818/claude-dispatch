import asyncio
import json
import os

import pytest

import server as srv

UUID = "PANE-UUID"


class BgaRequest:
    def __init__(self, query=None):
        self.query = query or {}


@pytest.fixture
def bga_env(tmp_path, monkeypatch):
    """A fake ~/.claude/projects/<proj>/<sid>.jsonl session with one background
    subagent transcript + sidecar meta, wired up through read_fleet_files()."""
    projects = tmp_path / "projects"
    proj_dir = projects / "enc-proj"
    proj_dir.mkdir(parents=True)
    sid = "session1"
    transcript = proj_dir / f"{sid}.jsonl"
    transcript.write_text("")
    sdir = proj_dir / f"{sid}/subagents"
    sdir.mkdir(parents=True)

    monkeypatch.setattr(srv, "_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(srv, "authed", lambda request: True)
    monkeypatch.setattr(srv, "read_fleet_files", lambda: {
        UUID: {"provider": "claude", "transcript": str(transcript)}})
    monkeypatch.setattr(srv, "_BGA_NOTE", {})
    monkeypatch.setattr(srv, "_BGA_LOG", {})
    return {"transcript": transcript, "sdir": sdir}


def _write_agent(sdir, agent_id, lines, meta):
    (sdir / f"agent-{agent_id}.jsonl").write_text("\n".join(lines) + "\n")
    (sdir / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))


def _assistant_line(mid, text=None, tool=None, tool_id=None, ts="2026-01-01T00:00:01Z"):
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool is not None:
        content.append({"type": "tool_use", "id": tool_id, "name": tool, "input": {}})
    return json.dumps({
        "type": "assistant", "timestamp": ts,
        "message": {"id": mid, "model": "claude-x", "content": content,
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 0}},
    })


def _tool_result_line(tool_id, is_error=False, text="ok", ts="2026-01-01T00:00:02Z"):
    return json.dumps({
        "type": "user", "timestamp": ts,
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id,
                                   "is_error": is_error,
                                   "content": [{"type": "text", "text": text}]}]},
    })


def _notification_line(task_id, status, result="done", tokens=100, tools=1,
                        dur_ms=500, ts="2026-01-01T00:00:03Z"):
    body = (f"<task-notification><task-id>{task_id}</task-id>"
            f"<status>{status}</status><result>{result}</result>"
            f"<usage><subagent_tokens>{tokens}</subagent_tokens>"
            f"<tool_uses>{tools}</tool_uses>"
            f"<duration_ms>{dur_ms}</duration_ms></usage></task-notification>")
    return json.dumps({"type": "queue-operation", "timestamp": ts, "text": body})


# ── notification parsing ────────────────────────────────────────────────────

def test_notes_parse_queue_operation_and_user_lines(bga_env, tmp_path):
    transcript = bga_env["transcript"]
    transcript.write_text(
        _notification_line("task-1", "completed") + "\n"
        + json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:04Z",
                       "text": _notification_line("task-2", "failed", result="boom")})
        + "\n")
    notes = srv._bga_notes(str(transcript))
    assert notes["task-1"]["status"] == "completed"
    assert notes["task-1"]["tokens"] == 100
    assert "task-2" in notes
    assert notes["task-2"]["status"] == "failed"


def test_notes_newest_notification_wins_on_resume(bga_env, tmp_path):
    transcript = bga_env["transcript"]
    transcript.write_text(
        _notification_line("task-1", "completed", ts="2026-01-01T00:00:03Z") + "\n")
    first = srv._bga_notes(str(transcript))
    assert first["task-1"]["status"] == "completed"
    with open(transcript, "a") as fh:
        fh.write(_notification_line("task-1", "failed",
                                     ts="2026-01-01T01:00:00Z") + "\n")
    second = srv._bga_notes(str(transcript))
    assert second["task-1"]["status"] == "failed"


# ── resume detection ─────────────────────────────────────────────────────────

def test_status_running_again_after_notification_if_mtime_newer(bga_env):
    meta = {"stoppedByUser": False}
    notif = {"status": "completed", "ts": 1000.0}
    assert srv._bga_status(meta, notif, mtime=1010.0, now=1011.0) == "running"
    assert srv._bga_status(meta, notif, mtime=1002.0, now=1011.0) == "completed"


def test_status_stale_when_never_notified_and_old(bga_env):
    meta = {}
    now = 10_000.0
    assert srv._bga_status(meta, None, mtime=now - 1801, now=now) == "stale"
    assert srv._bga_status(meta, None, mtime=now - 10, now=now) == "running"


def test_status_stopped_by_user_overrides_stale_guess(bga_env):
    meta = {"stoppedByUser": True}
    now = 10_000.0
    assert srv._bga_status(meta, None, mtime=now - 5, now=now) == "stopped"


# ── since/stable cursor ──────────────────────────────────────────────────────

def test_log_since_and_stable_cursor(bga_env):
    sdir = bga_env["sdir"]
    lines = [
        json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z",
                     "message": {"content": "do the thing"}}),
        _assistant_line("m1", text="working on it"),
        _assistant_line("m2", tool="Bash", tool_id="t1"),
    ]
    _write_agent(sdir, "abc123", lines,
                 {"agentType": "general", "description": "d", "toolUseId": "tu1",
                  "requestShape": "background"})
    resp = asyncio.run(srv.api_bga_log(BgaRequest({"uuid": UUID, "id": "abc123",
                                                     "since": "1"})))
    body = json.loads(resp.text)
    assert body["from"] == 1
    assert len(body["events"]) == 1
    # tool_use has no result yet -> stable stops at its index
    assert body["stable"] == 1

    # resolve the pending tool call; stable should now reach the end
    with open(sdir / "agent-abc123.jsonl", "a") as fh:
        fh.write(_tool_result_line("t1") + "\n")
    resp2 = asyncio.run(srv.api_bga_log(BgaRequest({"uuid": UUID, "id": "abc123"})))
    body2 = json.loads(resp2.text)
    assert body2["stable"] == len(body2["events"])
    assert body2["events"][-1]["ok"] is True


# ── traversal rejection ──────────────────────────────────────────────────────

def test_log_rejects_path_traversal_id(bga_env):
    resp = asyncio.run(srv.api_bga_log(BgaRequest({"uuid": UUID, "id": "../x"})))
    assert resp.status == 403


def test_log_rejects_unknown_id(bga_env):
    resp = asyncio.run(srv.api_bga_log(BgaRequest({"uuid": UUID, "id": "doesnotexist"})))
    assert resp.status == 403
