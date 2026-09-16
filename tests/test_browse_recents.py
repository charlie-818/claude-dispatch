"""Recent working dirs for the spawn directory picker."""
import asyncio
import json
import os
from urllib.parse import quote

import pytest

import server as srv


class CommandRequest(dict):
    def __init__(self, body=None, query=None):
        super().__init__()
        self.body = body or {}
        self.query = query or {}

    async def json(self):
        return self.body


@pytest.fixture
def recents_file(tmp_path, monkeypatch):
    p = tmp_path / ".recent_dirs.json"
    monkeypatch.setattr(srv, "RECENTS_FILE", p)
    monkeypatch.setattr(srv, "GROK_SESSIONS", str(tmp_path / "no-grok-sessions"))
    monkeypatch.setattr(srv, "read_fleet_files", lambda: {})
    monkeypatch.setattr(srv, "_history_result",
                        {"at": 0, "data": None, "building": False})
    return p


@pytest.fixture
def allow_tmp(monkeypatch):
    """pytest tmp_path lives under /var/folders or /tmp, which _is_scratch skips."""
    monkeypatch.setattr(srv, "_is_scratch", lambda p: False)


@pytest.fixture
def command_api(monkeypatch):
    monkeypatch.setattr(srv.auth, "same_origin", lambda request: True)
    monkeypatch.setattr(srv.auth, "unlocked", lambda request: {})
    monkeypatch.setattr(srv.auth, "audit", lambda *args: None)
    monkeypatch.setattr(srv, "note_activity", lambda: None)


def test_note_recent_dir_prepends_and_dedupes(tmp_path, recents_file, allow_tmp):
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    a.mkdir()
    b.mkdir()
    srv._note_recent_dir(str(a))
    srv._note_recent_dir(str(b))
    data = json.loads(recents_file.read_text())
    assert [d["path"] for d in data] == [str(b), str(a)]
    assert data[0]["name"] == "beta"
    srv._note_recent_dir(str(a))
    data = json.loads(recents_file.read_text())
    assert [d["path"] for d in data] == [str(a), str(b)]
    assert data[0]["name"] == "alpha"


def test_note_recent_dir_skips_scratch(tmp_path, recents_file):
    scratch = tmp_path / "cc-scratch-xyz"
    scratch.mkdir()
    srv._note_recent_dir(str(scratch))
    srv._note_recent_dir("/tmp")
    srv._note_recent_dir("/tmp/anything")
    srv._note_recent_dir("/var/folders/zz/T/work")
    srv._note_recent_dir(str(tmp_path))
    assert not recents_file.exists()


def test_recent_dirs_merges_file_and_fleet(tmp_path, recents_file, allow_tmp, monkeypatch):
    older = tmp_path / "older-proj"
    newer = tmp_path / "newer-proj"
    older.mkdir()
    newer.mkdir()
    recents_file.write_text(json.dumps([
        {"path": str(older), "name": "older-proj", "last": 10},
        {"path": str(newer), "name": "newer-proj", "last": 20},
    ]))
    monkeypatch.setattr(srv, "read_fleet_files", lambda: {
        "PANE": {"cwd": str(newer), "mtime": 50, "state_since": 40},
    })
    out = srv._recent_dirs()
    assert [d["path"] for d in out] == [str(newer), str(older)]
    assert out[0]["last"] == 50
    assert out[0]["name"] == "newer-proj"


def test_recent_dirs_grok_session_fallback(tmp_path, recents_file, allow_tmp, monkeypatch):
    proj = tmp_path / "tokenized-staking"
    proj.mkdir()
    sess = tmp_path / "grok-sessions"
    child = sess / quote(str(proj), safe="")
    child.mkdir(parents=True)
    os.utime(child, (100, 100))
    monkeypatch.setattr(srv, "GROK_SESSIONS", str(sess))
    out = srv._recent_dirs()
    hit = next(d for d in out if d["path"] == str(proj))
    assert hit["name"] == "tokenized-staking"
    assert hit["last"] == 100


def test_api_browse_empty_path_returns_recents(command_api, monkeypatch):
    fake = [{"name": "tokenized-staking",
             "path": "/Users/charliebc/tokenized-staking", "last": 1}]
    monkeypatch.setattr(srv, "_recent_dirs", lambda: fake)
    monkeypatch.setattr(srv, "_browse_roots", lambda: [])
    monkeypatch.setattr(srv, "installed_agents", lambda: ["claude"])
    response = asyncio.run(srv.api_browse(CommandRequest(query={})))
    assert response.status == 200
    body = json.loads(response.text)
    assert body["recents"] == fake
    assert body["path"] == ""
    assert body["roots"] == []
