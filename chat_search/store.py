"""Storage: one SQLite file (sessions, messages, FTS5 index, API tokens) plus
the raw transcript files, all under one data directory."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

KINDS = ("human", "assistant", "thinking", "tool_use", "tool_result", "system", "summary")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    filename    TEXT NOT NULL,
    title       TEXT,
    project     TEXT,
    git_branch  TEXT,
    version     TEXT,
    first_ts    TEXT,
    last_ts     TEXT,
    n_messages  INTEGER NOT NULL DEFAULT 0,
    n_bytes     INTEGER NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL,
    owner       TEXT NOT NULL DEFAULT 'local'
);
CREATE UNIQUE INDEX IF NOT EXISTS sessions_owner_session ON sessions(owner, session_id);
CREATE INDEX IF NOT EXISTS sessions_sha ON sessions(sha256);
CREATE TABLE IF NOT EXISTS users (
    name        TEXT PRIMARY KEY,
    token_hash  TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    session_pk  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    uuid        TEXT,
    ts          TEXT,
    kind        TEXT NOT NULL,
    tool        TEXT,
    text        TEXT NOT NULL,
    is_error    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_pk, seq);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


class QuotaExceeded(Exception):
    def __init__(self, used: int, limit: int, incoming: int):
        self.used, self.limit, self.incoming = used, limit, incoming
        super().__init__("storage quota exceeded: %d used + %d incoming > %d limit" % (used, incoming, limit))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_text(content) -> str:
    """Flatten a content block's payload (str or list of parts) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                if part.get("type") == "text":
                    out.append(part.get("text") or "")
                elif part.get("type") == "image":
                    out.append("[image]")
                else:
                    out.append(json.dumps(part, ensure_ascii=False))
        return "\n".join(out)
    return json.dumps(content, ensure_ascii=False)


def _tool_input_text(inp) -> str:
    """A tool call's input as it was written, not JSON-escaped: a Bash call
    shows its command and description, an Edit its path and strings. Nested
    values fall back to JSON."""
    if isinstance(inp, str):
        return inp
    if not isinstance(inp, dict):
        return json.dumps(inp, ensure_ascii=False, indent=2)
    if "command" in inp and isinstance(inp["command"], str):
        out = [inp["command"]]
        if inp.get("description"):
            out.append("# %s" % inp["description"])
        rest = {k: v for k, v in inp.items() if k not in ("command", "description")}
        if rest:
            out.append(json.dumps(rest, ensure_ascii=False))
        return "\n".join(out)
    out = []
    for k, v in inp.items():
        if isinstance(v, str):
            out.append("%s: %s" % (k, v) if "\n" not in v else "%s:\n%s" % (k, v))
        else:
            out.append("%s: %s" % (k, json.dumps(v, ensure_ascii=False)))
    return "\n".join(out)


def iter_messages(fh):
    """Yield (record_dict, message_dict) pairs from a Claude Code transcript,
    one per visible unit: a human prompt, an assistant text, a thinking block,
    a tool call, a tool result, a system note. Tool calls and results are split
    out of their envelope so each is searchable and renderable on its own."""
    for raw in fh:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = rec.get("type")
        ts = rec.get("timestamp")
        if t == "summary":
            yield rec, {"kind": "summary", "ts": ts, "text": rec.get("summary") or "", "tool": None}
        elif t == "system":
            txt = rec.get("content") or rec.get("message") or ""
            if isinstance(txt, dict):
                txt = _as_text(txt.get("content"))
            sub = rec.get("subtype") or ""
            if sub == "turn_duration":
                continue                        # bookkeeping, not conversation
            yield rec, {"kind": "system", "ts": ts, "text": str(txt), "tool": sub or None}
        elif t in ("user", "assistant"):
            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                if content.strip():
                    yield rec, {"kind": "human" if t == "user" else "assistant", "ts": ts,
                                "text": content, "tool": None}
                continue
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                pt = part.get("type")
                if pt == "text":
                    txt = part.get("text") or ""
                    if txt.strip():
                        yield rec, {"kind": "human" if t == "user" else "assistant", "ts": ts,
                                    "text": txt, "tool": None}
                elif pt == "thinking":
                    txt = part.get("thinking") or ""
                    if txt.strip():
                        yield rec, {"kind": "thinking", "ts": ts, "text": txt, "tool": None}
                elif pt == "tool_use":
                    txt = _tool_input_text(part.get("input"))
                    yield rec, {"kind": "tool_use", "ts": ts, "text": txt, "tool": part.get("name"),
                                "uuid": part.get("id")}
                elif pt == "tool_result":
                    yield rec, {"kind": "tool_result", "ts": ts, "text": _as_text(part.get("content")),
                                "tool": part.get("tool_use_id"), "is_error": 1 if part.get("is_error") else 0}
                elif pt == "image":
                    yield rec, {"kind": "human", "ts": ts, "text": "[image]", "tool": None}


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.uploads = self.data_dir / "uploads"
        self.db_path = self.data_dir / "index.sqlite"

    def init(self):
        self.uploads.mkdir(parents=True, exist_ok=True)
        with self.db() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            if cols and "owner" not in cols:          # index created before per-user ownership
                conn.execute("ALTER TABLE sessions ADD COLUMN owner TEXT NOT NULL DEFAULT 'local'")
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'sessions'").fetchone()
            if ddl and "UNIQUE" in ddl[0]:            # sha256 used to be globally unique; it is per owner now
                # RENAME also rewrites messages' FK to point at sessions_old, so the
                # DROP would cascade-delete every message: do it with FKs off and
                # rebuild the child table's FK by recreating messages from a copy.
                conn.executescript("""
                    PRAGMA foreign_keys = OFF;
                    PRAGMA legacy_alter_table = ON;
                    ALTER TABLE sessions RENAME TO sessions_old;
                    %s
                    INSERT INTO sessions SELECT * FROM sessions_old;
                    DROP TABLE sessions_old;
                    PRAGMA legacy_alter_table = OFF;
                    PRAGMA foreign_keys = ON;""" % SCHEMA.split("CREATE UNIQUE INDEX")[0])
            conn.executescript(SCHEMA)

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def usage(self, conn, owner: str) -> int:
        """Bytes of raw transcript held for `owner` (what quotas count)."""
        return conn.execute("SELECT COALESCE(SUM(n_bytes),0) FROM sessions WHERE owner = ?", (owner,)).fetchone()[0]

    def ingest(self, path: Path, filename: str, owner: str = "local", limit: int | None = None) -> tuple[int, bool]:
        """Index one transcript file for `owner`. Returns (session_pk, created).
        The same bytes again are a no-op; a newer copy of a session already held
        (same sessionId, different bytes -- a transcript grows) replaces it.
        `limit` (bytes) is the owner's storage quota; None = unlimited."""
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        with self.db() as conn:
            row = conn.execute("SELECT id FROM sessions WHERE sha256 = ? AND owner = ?", (sha, owner)).fetchone()
            if row:
                return row["id"], False
        meta = {"session_id": None, "agent_id": None, "title": None, "project": None, "git_branch": None, "version": None}
        first_ts = last_ts = None
        rows = []
        import io
        with io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="replace") as fh:
            # Titles live in their own records; scan them alongside the messages.
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                meta["session_id"] = meta["session_id"] or rec.get("sessionId")
                meta["agent_id"] = meta["agent_id"] or rec.get("agentId")
                if rec.get("type") == "custom-title" and rec.get("customTitle"):
                    meta["title"] = rec["customTitle"]
                elif rec.get("type") == "ai-title" and rec.get("aiTitle") and not meta["title"]:
                    meta["title"] = rec["aiTitle"]
                elif rec.get("type") == "summary" and rec.get("summary") and not meta["title"]:
                    meta["title"] = rec["summary"]
                for k, src in (("project", "cwd"), ("git_branch", "gitBranch"), ("version", "version")):
                    if rec.get(src) and not meta[k]:
                        meta[k] = rec[src]
            fh.seek(0)
            for seq, (rec, m) in enumerate(iter_messages(fh)):
                ts = m.get("ts")
                if ts:
                    first_ts = min(first_ts, ts) if first_ts else ts
                    last_ts = max(last_ts, ts) if last_ts else ts
                rows.append((seq, m.get("uuid") or rec.get("uuid"), ts, m["kind"], m.get("tool"),
                             m["text"], m.get("is_error", 0)))
        if not meta["title"]:
            first_human = next((r[5] for r in rows if r[3] == "human"), "")
            meta["title"] = (first_human.strip().splitlines() or [""])[0][:120] or filename
        session_id = meta["session_id"] or sha[:12]
        if meta["agent_id"]:                       # a subagent's transcript sits beside its parent's
            session_id = "%s/agent-%s" % (session_id, meta["agent_id"])
            meta["title"] = "↳ subagent: " + (meta["title"] or "")
        with self.db() as conn:
            old = conn.execute("SELECT id, sha256, n_bytes FROM sessions WHERE owner = ? AND session_id = ?",
                               (owner, session_id)).fetchone()
            if limit is not None:
                used = self.usage(conn, owner) - (old["n_bytes"] if old else 0)
                if used + len(data) > limit:
                    raise QuotaExceeded(used, limit, len(data))
            dest = self.uploads / f"{sha}.jsonl"
            if not dest.exists():
                dest.write_bytes(data)
            if old:
                conn.execute("DELETE FROM messages WHERE session_pk = ?", (old["id"],))
                conn.execute("DELETE FROM sessions WHERE id = ?", (old["id"],))
                if old["sha256"] != sha and not conn.execute(
                        "SELECT 1 FROM sessions WHERE sha256 = ?", (old["sha256"],)).fetchone():
                    (self.uploads / f"{old['sha256']}.jsonl").unlink(missing_ok=True)
            cur = conn.execute(
                "INSERT INTO sessions (session_id, sha256, filename, title, project, git_branch, version,"
                " first_ts, last_ts, n_messages, n_bytes, uploaded_at, owner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, sha, filename, meta["title"], meta["project"],
                 meta["git_branch"], meta["version"], first_ts, last_ts, len(rows), len(data), now_iso(), owner))
            pk = cur.lastrowid
            conn.executemany(
                "INSERT INTO messages (session_pk, seq, uuid, ts, kind, tool, text, is_error)"
                " VALUES (?,?,?,?,?,?,?,?)", [(pk, *r) for r in rows])
        return pk, True

    def delete_session(self, conn, s) -> None:
        conn.execute("DELETE FROM messages WHERE session_pk = ?", (s["id"],))
        conn.execute("DELETE FROM sessions WHERE id = ?", (s["id"],))
        if not conn.execute("SELECT 1 FROM sessions WHERE sha256 = ?", (s["sha256"],)).fetchone():
            (self.uploads / f"{s['sha256']}.jsonl").unlink(missing_ok=True)

    def delete_owner(self, owner: str) -> int:
        """Everything an owner holds (account deletion). Returns sessions removed."""
        with self.db() as conn:
            rows = conn.execute("SELECT id, sha256 FROM sessions WHERE owner = ?", (owner,)).fetchall()
            for s in rows:
                self.delete_session(conn, s)
            conn.execute("DELETE FROM users WHERE name = ?", (owner,))
        return len(rows)
