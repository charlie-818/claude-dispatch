"""Usage + history scanners for Claude/Codex/Grok."""
import json
import os
import time
from datetime import datetime

import pytest

import server as srv


def _now_iso():
    return datetime.now().astimezone().isoformat()


def _write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


@pytest.fixture(autouse=True)
def _isolate_caches():
    srv._native_scan_cache.clear()
    srv._hist_cache.clear()
    srv._usage_cache.clear()
    yield
    srv._native_scan_cache.clear()
    srv._hist_cache.clear()
    srv._usage_cache.clear()


def test_human_prompt_skips_codex_agents_md():
    txt = "# AGENTS.md instructions\n\n<INSTRUCTIONS>\nDelegate by default."
    assert srv.human_prompt(txt) == ""


def test_claude_history_skips_labeler(tmp_path):
    p = tmp_path / "sess.jsonl"
    p.write_text(json.dumps({
        "type": "user", "cwd": "/tmp/x", "timestamp": _now_iso(),
        "message": {"role": "user",
                    "content": [{"type": "text",
                                 "text": "You are labeling a coding session for a phone status line"}]},
    }) + "\n")
    assert srv._scan_history_file(str(p)) is None


def test_codex_rate_limits_maps_windows():
    future = time.time() + 3600
    rl = {
        "primary": {"used_percent": 87.0, "window_minutes": 300, "resets_at": future},
        "secondary": {"used_percent": 14.0, "window_minutes": 10080, "resets_at": future + 9},
    }
    out = srv._codex_rate_limits(rl)
    assert out["five_hour"]["used_percentage"] == 87.0
    assert out["seven_day"]["used_percentage"] == 14.0
    expired = {
        "primary": {"used_percent": 99, "resets_at": time.time() - 10},
        "secondary": None,
    }
    assert srv._codex_rate_limits(expired) == {}
    monthly = {
        "primary": {"used_percent": 22.0, "window_minutes": 43200, "resets_at": future},
        "secondary": None,
    }
    mo = srv._codex_rate_limits(monthly)
    assert mo["monthly"]["used_percentage"] == 22.0
    assert "five_hour" not in mo


def test_claude_account_limits_ignores_pane_stale(tmp_path, monkeypatch):
    future = time.time() + 3600
    claude = tmp_path / "claude.json"
    claude.write_text(json.dumps({
        "provider": "claude",
        "rate_limits": {
            "five_hour": {"used_percentage": 40, "resets_at": future},
            "seven_day": {"used_percentage": 12, "resets_at": future},
        },
    }))
    past = time.time() - 3600
    os.utime(claude, (past, past))
    (tmp_path / "grok.json").write_text(json.dumps({
        "provider": "grok",
        "rate_limits": {
            "five_hour": {"used_percentage": 99, "resets_at": future},
        },
    }))
    monkeypatch.setattr(srv, "FLEET_DIR", str(tmp_path))
    out = srv._claude_account_limits()
    assert out["five_hour"]["used_percentage"] == 40
    assert out["seven_day"]["used_percentage"] == 12


def test_scan_codex_file(tmp_path):
    sid = "01a09d7b-2938-7bd1-940f-4e70d04ed3c3"
    future = time.time() + 7200
    path = tmp_path / f"rollout-2026-09-13T18-14-49-{sid}.jsonl"
    ts = _now_iso()
    _write(path, [
        {"timestamp": ts, "type": "session_meta",
         "payload": {"session_id": sid, "cwd": "/Users/x/proj"}},
        {"timestamp": ts, "type": "turn_context",
         "payload": {"model": "gpt-5.6-terra", "cwd": "/Users/x/proj"}},
        {"timestamp": ts, "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": "# AGENTS.md instructions\n<INSTRUCTIONS>"}]}},
        {"timestamp": ts, "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "fix the picker"}]}},
        {"timestamp": ts, "type": "token_usage_record",
         "payload": {"usage": {"input_tokens": 1000, "cached_input_tokens": 800,
                               "cache_write_input_tokens": 0, "output_tokens": 50}}},
        {"timestamp": ts, "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "apply_patch",
                     "input": "*** Begin Patch\n*** Update File: app.py\n@@\n-old\n+new\n+extra\n*** End Patch"}},
        {"timestamp": ts, "type": "event_msg",
         "payload": {"type": "token_count",
                     "rate_limits": {
                         "primary": {"used_percent": 40, "resets_at": future},
                         "secondary": {"used_percent": 10, "resets_at": future}}}},
    ])
    pack = srv._scan_codex_file(str(path))
    hist = pack["hist"]
    assert hist["provider"] == "codex"
    assert hist["session_id"] == sid
    assert hist["title"] == "fix the picker"
    assert hist["prompts"] == 1
    assert hist["cwd"] == "/Users/x/proj"
    assert hist["project"] == "proj"
    assert hist["model"] == "gpt-5.6-terra"
    assert hist["tokens"] == 250          # (1000-800)+50
    assert hist["cost"] == 0.0
    assert hist["files"] == 1
    assert hist["lines_add"] == 2
    assert hist["lines_del"] == 1
    assert pack["limits"]["five_hour"]["used_percentage"] == 40
    day = next(iter(pack["days"]))
    assert pack["days"][day]["tokens"] == 250


def test_scan_grok_session(tmp_path):
    sid = "01a09d8f-81f5-7b03-942b-7d1b25ea15c7"
    sdir = tmp_path / "cwd" / sid
    sdir.mkdir(parents=True)
    (sdir / "summary.json").write_text(json.dumps({
        "info": {"id": sid, "cwd": "/Users/x/claude-dispatch"},
        "generated_title": "Fix the usage tabs",
        "session_summary": "usage for three agents",
        "current_model_id": "grok-4.6-build",
        "created_at": _now_iso(),
        "last_active_at": _now_iso(),
    }))
    (sdir / "usage.json").write_text(json.dumps({
        "sessionId": sid,
        "session": {
            "inputTokens": 1000, "outputTokens": 100,
            "cachedReadTokens": 400, "cacheCreationTokens": 0,
            "costUsdTicks": 25 * 10**10, "primaryModelId": "grok-4.6-build",
        },
        "turns": [{
            "turnNumber": 1,
            "endedAt": _now_iso(),
            "inputTokens": 1000, "outputTokens": 100,
            "cachedReadTokens": 400, "cacheCreationTokens": 0,
            "costUsdTicks": 25 * 10**10,
        }],
    }))
    _write(sdir / "chat_history.jsonl", [
        {"type": "user", "content": [{"type": "text", "text": "wire grok usage"}]},
        {"type": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ])
    _write(sdir / "hunk_records.jsonl", [{
        "authorType": "agent", "eventType": "updated", "filePath": "app.py",
        "linesAdded": 4, "linesRemoved": 2,
    }])
    pack = srv._scan_grok_session(str(sdir))
    hist = pack["hist"]
    assert hist["provider"] == "grok"
    assert hist["session_id"] == sid
    assert hist["title"] == "Fix the usage tabs"
    assert hist["model"] == "grok-4.6"
    assert hist["prompts"] == 1
    assert hist["tokens"] == 700          # (1000-400)+100
    assert hist["cost"] == 25.0
    assert hist["files"] == 1
    assert hist["lines_add"] == 4
    assert hist["lines_del"] == 2
    assert "wire grok usage" in hist["search"]
    day = next(iter(pack["days"]))
    assert pack["days"][day]["cost"] == 25.0
    assert pack["days"][day]["tokens"] == 700


def test_scan_current_codex_token_count_and_cost(tmp_path):
    sid = "01a09d7b-2938-7bd1-940f-4e70d04ed3c5"
    path = tmp_path / f"rollout-2026-09-13T18-14-49-{sid}.jsonl"
    ts = _now_iso()
    usage = {"input_tokens": 1_000_000, "cached_input_tokens": 800_000,
             "cache_write_input_tokens": 100_000, "output_tokens": 50_000}
    _write(path, [
        {"timestamp": ts, "type": "session_meta",
         "payload": {"session_id": sid, "cwd": "/tmp/c"}},
        {"timestamp": ts, "type": "turn_context",
         "payload": {"model": "gpt-5.6-terra"}},
        {"timestamp": ts, "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "last_token_usage": usage, "total_token_usage": usage}}},
    ])
    pack = srv._scan_codex_file(str(path))
    assert pack["hist"]["tokens"] == 350_000
    assert pack["hist"]["cost"] == 1.41
    assert next(iter(pack["days"].values()))["cost"] == pytest.approx(1.41)

    live = srv.codex_ops(str(path))
    assert live["tokens"] == 350_000
    assert live["cost"] == pytest.approx(1.41)


def test_usage_from_packs_fills_30_days():
    today = time.strftime("%Y-%m-%d")
    packs = [{
        "hist": {"tokens": 10, "cost": 1.5, "session_id": "a"},
        "days": {today: {"tokens": 10, "cost": 1.5}},
        "limits": {"five_hour": {"used_percentage": 3, "resets_at": time.time() + 100}},
    }]
    out = srv._usage_from_packs(packs)
    assert len(out["daily"]) == 30
    assert out["daily"][-1]["d"] == today
    assert out["today"]["tokens"] == 10
    assert out["today"]["cost"] == 1.5
    assert out["all_time"]["sessions"] == 1
    assert out["all_time"]["active_days"] == 1
    assert out["limits"]["five_hour"]["used_percentage"] == 3


def test_compute_usage_codex_and_grok(tmp_path, monkeypatch):
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    cdir = tmp_path / "codex" / "2026" / "09" / "13"
    _write(cdir / f"rollout-2026-09-13T01-00-00-{sid}.jsonl", [
        {"timestamp": _now_iso(), "type": "session_meta",
         "payload": {"session_id": sid, "cwd": "/tmp/c"}},
        {"timestamp": _now_iso(), "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hello codex"}]}},
        {"timestamp": _now_iso(), "type": "token_usage_record",
         "payload": {"usage": {"input_tokens": 20, "cached_input_tokens": 0,
                               "cache_write_input_tokens": 0, "output_tokens": 5}}},
    ])
    gsid = "01aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee0"
    gdir = tmp_path / "grok" / "%2Ftmp" / gsid
    gdir.mkdir(parents=True)
    (gdir / "summary.json").write_text(json.dumps({
        "info": {"id": gsid, "cwd": "/tmp/g"},
        "generated_title": "Grok chat",
        "current_model_id": "grok-4.6",
        "created_at": _now_iso(),
        "last_active_at": _now_iso(),
    }))
    (gdir / "usage.json").write_text(json.dumps({
        "session": {"inputTokens": 8, "outputTokens": 2,
                    "cachedReadTokens": 0, "cacheCreationTokens": 0,
                    "costUsdTicks": 5 * 10**10},
        "turns": [{"endedAt": _now_iso(),
                   "inputTokens": 8, "outputTokens": 2,
                   "cachedReadTokens": 0, "cacheCreationTokens": 0,
                   "costUsdTicks": 5 * 10**10}],
    }))
    _write(gdir / "chat_history.jsonl", [
        {"type": "user", "content": [{"type": "text", "text": "hi grok"}]},
    ])

    monkeypatch.setattr(srv, "CODEX_SESSIONS", str(tmp_path / "codex"))
    monkeypatch.setattr(srv, "GROK_SESSIONS", str(tmp_path / "grok"))
    monkeypatch.setattr(srv, "GROK_LOG", str(tmp_path / "no-such-log.jsonl"))
    monkeypatch.setattr(srv, "_PROJECTS_DIR", str(tmp_path / "claude"))

    cu = srv._compute_usage("codex")
    assert cu["all_time"]["sessions"] == 1
    assert cu["all_time"]["tokens"] == 25
    assert cu["today"]["tokens"] == 25

    gu = srv._compute_usage("grok")
    assert gu["all_time"]["sessions"] == 1
    assert gu["all_time"]["cost"] == 5.0
    assert gu["limits"] == {}

    hist = srv._build_history(limit=10)
    providers = {e["provider"] for e in hist}
    assert providers == {"codex", "grok"}
    by_p = {e["provider"]: e for e in hist}
    assert by_p["codex"]["title"] == "hello codex"
    assert by_p["grok"]["title"] == "Grok chat"


def test_scan_codex_file_clears_stale_limits(tmp_path):
    sid = "01a09d7b-2938-7bd1-940f-4e70d04ed3c4"
    future = time.time() + 7200
    path = tmp_path / f"rollout-2026-09-13T18-14-49-{sid}.jsonl"
    ts = _now_iso()
    _write(path, [
        {"timestamp": ts, "type": "session_meta",
         "payload": {"session_id": sid, "cwd": "/tmp/c"}},
        {"timestamp": ts, "type": "event_msg",
         "payload": {"type": "token_count",
                     "rate_limits": {
                         "primary": {"used_percent": 100, "window_minutes": 300,
                                     "resets_at": future},
                         "secondary": {"used_percent": 10, "resets_at": future}}}},
        {"timestamp": ts, "type": "event_msg",
         "payload": {"type": "token_count",
                     "rate_limits": {"primary": None, "secondary": None}}},
    ])
    pack = srv._scan_codex_file(str(path))
    assert pack["limits"] == {}


def _write_grok_session(sdir, sid, cwd, title, tokens_in, tokens_out, prompt):
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "summary.json").write_text(json.dumps({
        "info": {"id": sid, "cwd": cwd},
        "generated_title": title,
        "current_model_id": "grok-4.6",
        "created_at": _now_iso(),
        "last_active_at": _now_iso(),
    }))
    (sdir / "usage.json").write_text(json.dumps({
        "session": {"inputTokens": tokens_in, "outputTokens": tokens_out,
                    "cachedReadTokens": 0, "cacheCreationTokens": 0,
                    "costUsdTicks": 0},
        "turns": [{"endedAt": _now_iso(),
                   "inputTokens": tokens_in, "outputTokens": tokens_out,
                   "cachedReadTokens": 0, "cacheCreationTokens": 0,
                   "costUsdTicks": 0}],
    }))
    _write(sdir / "chat_history.jsonl", [
        {"type": "user", "content": [{"type": "text", "text": prompt}]},
    ])


def test_build_history_skips_grok_children(tmp_path, monkeypatch):
    parent_id = "01aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1"
    child_id = "01bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    root = tmp_path / "grok" / "%2Ftmp"
    parent = root / parent_id
    child = root / child_id
    _write_grok_session(parent, parent_id, "/tmp/g", "Parent chat", 100, 20, "parent prompt")
    _write_grok_session(child, child_id, "/tmp/g", "Child chat", 400, 50, "child prompt")
    (parent / "subagents" / child_id).mkdir(parents=True)

    monkeypatch.setattr(srv, "CODEX_SESSIONS", str(tmp_path / "codex"))
    monkeypatch.setattr(srv, "GROK_SESSIONS", str(tmp_path / "grok"))
    monkeypatch.setattr(srv, "GROK_LOG", str(tmp_path / "no-such-log.jsonl"))
    monkeypatch.setattr(srv, "_PROJECTS_DIR", str(tmp_path / "claude"))

    hist = srv._build_history(limit=10)
    ids = {e["session_id"] for e in hist}
    assert parent_id in ids
    assert child_id not in ids

    gu = srv._compute_usage("grok")
    assert gu["all_time"]["tokens"] == 570   # parent 120 + child 450


def test_build_history_per_provider_cap(tmp_path, monkeypatch):
    old = datetime.fromtimestamp(time.time() - 86400).astimezone().isoformat()
    new = _now_iso()
    claude_dir = tmp_path / "claude" / "proj"
    claude_dir.mkdir(parents=True)
    (claude_dir / "aaaaaaaa-1111-2222-3333-444444444444.jsonl").write_text(
        json.dumps({
            "type": "user", "cwd": "/tmp/x", "timestamp": old,
            "message": {"role": "user",
                        "content": [{"type": "text", "text": "old claude"}]},
        }) + "\n")
    cdir = tmp_path / "codex" / "2026" / "09" / "13"
    for i in range(3):
        sid = f"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeee{i:02d}"
        _write(cdir / f"rollout-2026-09-13T01-00-0{i}-{sid}.jsonl", [
            {"timestamp": new, "type": "session_meta",
             "payload": {"session_id": sid, "cwd": "/tmp/c"}},
            {"timestamp": new, "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": f"codex {i}"}]}},
        ])
    monkeypatch.setattr(srv, "CODEX_SESSIONS", str(tmp_path / "codex"))
    monkeypatch.setattr(srv, "GROK_SESSIONS", str(tmp_path / "grok"))
    monkeypatch.setattr(srv, "_PROJECTS_DIR", str(tmp_path / "claude"))

    hist = srv._build_history(limit=1)
    providers = [e["provider"] for e in hist]
    assert providers.count("codex") == 1
    assert providers.count("claude") == 1
    assert len(hist) == 2


def test_resume_cli_args():
    assert srv.resume_cli_args("claude", "abc") == "--resume abc"
    assert srv.resume_cli_args("codex", "abc") == "resume abc"
    assert srv.resume_cli_args("grok", "abc") == "--resume abc"
    import shlex
    assert srv.resume_cli_args("codex", "a b") == "resume " + shlex.quote("a b")


def test_grok_billing_limits_from_log(tmp_path, monkeypatch):
    end = time.time() + 8 * 3600
    log = tmp_path / "unified.jsonl"
    log.write_text(json.dumps({
        "msg": "billing: fetched credits config",
        "ctx": {"config": {
            "creditUsagePercent": 70.0,
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "end": datetime.fromtimestamp(end).astimezone().isoformat(),
            },
        }},
    }) + "\n")
    monkeypatch.setattr(srv, "GROK_LOG", str(log))
    lim = srv._grok_billing_limits()
    assert lim["weekly"]["used_percentage"] == 70.0
    assert lim["seven_day"]["used_percentage"] == 70.0
    assert lim["seven_day"]["resets_at"] is not None


def test_grok_session_limits_hottest_fresh(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = root / "proj" / "sess-a"
    b = root / "proj" / "sess-b"
    a.mkdir(parents=True); b.mkdir(parents=True)
    (a / "signals.json").write_text(json.dumps({"contextWindowUsage": 12}))
    (b / "signals.json").write_text(json.dumps({"contextWindowUsage": 41}))
    monkeypatch.setattr(srv, "GROK_SESSIONS", str(root))
    lim = srv._grok_session_limits()
    assert lim["session"]["used_percentage"] == 41


def test_history_record_filters_provider(tmp_path, monkeypatch):
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    cdir = tmp_path / "codex" / "2026" / "09" / "13"
    _write(cdir / f"rollout-x-{sid}.jsonl", [
        {"timestamp": _now_iso(), "type": "session_meta",
         "payload": {"session_id": sid, "cwd": "/tmp/c"}},
        {"timestamp": _now_iso(), "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "x"}]}},
    ])
    monkeypatch.setattr(srv, "CODEX_SESSIONS", str(tmp_path / "codex"))
    monkeypatch.setattr(srv, "GROK_SESSIONS", str(tmp_path / "grok"))
    monkeypatch.setattr(srv, "_PROJECTS_DIR", str(tmp_path / "claude"))
    rec = srv._history_record(sid, "codex")
    assert rec["provider"] == "codex"
    assert rec["cwd"] == "/tmp/c"
    assert srv._history_record(sid, "grok") is None
