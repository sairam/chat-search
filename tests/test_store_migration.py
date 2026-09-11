"""Regression: upgrading an index whose sessions.sha256 was globally UNIQUE
must keep every message. (RENAME TABLE rewrites messages' foreign key to the
renamed table, so dropping it with foreign_keys=ON cascaded and emptied the
index — 2026-09-11, claude-search.dot.com.in.)"""
import json
import sqlite3
from pathlib import Path

from chat_search.store import Store

OLD_SCHEMA = """
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL, title TEXT, project TEXT, git_branch TEXT, version TEXT,
    first_ts TEXT, last_ts TEXT, n_messages INTEGER NOT NULL DEFAULT 0,
    n_bytes INTEGER NOT NULL DEFAULT 0, uploaded_at TEXT NOT NULL);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    session_pk INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, uuid TEXT, ts TEXT, kind TEXT NOT NULL, tool TEXT,
    text TEXT NOT NULL, is_error INTEGER NOT NULL DEFAULT 0);
CREATE VIRTUAL TABLE messages_fts USING fts5(text, content='messages', content_rowid='id', tokenize='unicode61');
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
"""


def _old_index(path: Path):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO sessions(session_id, sha256, filename, uploaded_at, n_messages) VALUES ('s1','abc','a.jsonl','2026-01-01T00:00:00Z',2)")
    conn.executemany("INSERT INTO messages(session_pk, seq, kind, text) VALUES (1,?,?,?)",
                     [(0, "human", "please set up the linode box"), (1, "assistant", "done")])
    conn.commit(); conn.close()


def test_pre_owner_index_keeps_messages(tmp_path):
    _old_index(tmp_path / "index.sqlite")
    st = Store(tmp_path); st.init()
    with st.db() as c:
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'linode'").fetchone()[0] == 1
        assert c.execute("SELECT owner FROM sessions").fetchone()[0] == "local"
        assert "sessions_old" not in c.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    st.init()                                   # idempotent on the migrated index
    with st.db() as c:
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_fresh_ingest_and_dedupe(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "sessionId": "abc", "uuid": "1", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "hello linode"}},
        {"type": "assistant", "sessionId": "abc", "uuid": "2", "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
    ]))
    st = Store(tmp_path / "data"); st.init()
    pk, created = st.ingest(f, "t.jsonl", owner="u")
    assert created
    assert st.ingest(f, "t.jsonl", owner="u") == (pk, False)
    with st.db() as c:
        assert c.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'linode'").fetchone()[0] == 1
