"""chat-search — upload Claude Code session transcripts (.jsonl), render them
as HTML, and full-text search across all of them (SQLite FTS5).

`create_app()` builds the FastAPI app. Who the caller is, and how much they
may store, are pluggable so the same core serves a single-user local install
(everyone is "local"), a reverse-proxy-authenticated team install
(X-Remote-User), or a hosted multi-tenant service with accounts and quotas.
"""
import hashlib
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from markupsafe import Markup, escape

from .render import fmt_ts, fts_query, highlight, human_bytes, next_day, render_text
from .store import KINDS, QuotaExceeded, Store, now_iso

HERE = Path(__file__).parent
PAGE = 200            # messages per session page — a full session can be 10k+ messages / 100 MB
MAX_RENDER = 60_000   # chars of one message rendered inline; the rest is behind /session/{pk}/m/{seq}
HOOK_NAME = "chat-search-push"


class Unauthenticated(Exception):
    pass


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get(
        "CHAT_SEARCH_DATA", Path.home() / ".local" / "share" / "chat-search")))
    local_projects: Path = field(default_factory=lambda: Path(os.environ.get(
        "CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects")))
    # "none": everyone is the single user "local" (run it on your own machine).
    # "proxy": trust X-Remote-User set by an authenticating reverse proxy.
    auth: str = field(default_factory=lambda: os.environ.get("CHAT_SEARCH_AUTH", "none"))
    admins: set = field(default_factory=lambda: {
        u.strip() for u in os.environ.get("CHAT_SEARCH_ADMINS", "").split(",") if u.strip()})
    brand: str = field(default_factory=lambda: os.environ.get("CHAT_SEARCH_BRAND", "chat-search"))


def create_app(settings: Settings | None = None, *,
               identity: Callable[[Request], str | None] | None = None,
               quota: Callable[[str], int | None] | None = None,
               is_admin: Callable[[str], bool] | None = None,
               anonymous_home: Callable[[Request], Response] | None = None,
               login_url: str | None = None,
               template_dirs: tuple = (),
               nav_links: tuple = ()) -> FastAPI:
    """identity(request) -> user name or None (not signed in).
    quota(user) -> byte limit for that user, or None for unlimited.
    is_admin(user) -> may see every session (default: Settings.admins).
    anonymous_home(request) -> page to show on / when not signed in.
    login_url: where an unauthenticated browser is redirected (else 401)."""
    st = settings or Settings()
    store = Store(st.data_dir)
    app = FastAPI(title=st.brand, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.store = st, store
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.loader = ChoiceLoader([FileSystemLoader(str(d)) for d in template_dirs]
                                        + [FileSystemLoader(str(HERE / "templates"))])
    templates.env.filters["ts"] = fmt_ts
    templates.env.filters["bytes"] = human_bytes
    templates.env.globals.update(KINDS=KINDS, brand=st.brand, nav_links=nav_links, hook_name=HOOK_NAME)

    if identity is None:
        if st.auth == "proxy":
            identity = lambda r: (r.headers.get("x-remote-user") or "").strip() or None
        else:
            identity = lambda r: "local"
    if quota is None:
        quota = lambda user: None

    @app.on_event("startup")
    def _init():
        store.init()

    # ── who / scope ────────────────────────────────────────────────────────

    def who(request: Request) -> str:
        user = identity(request)
        if not user:
            raise Unauthenticated()
        return user

    _admin_override = is_admin

    def is_admin(user: str) -> bool:
        if _admin_override is not None:
            return bool(_admin_override(user))
        return user in st.admins or (st.auth == "none" and user == "local")

    def _scope(user: str):
        """WHERE fragment + args restricting sessions to what `user` may see."""
        return ("1=1", []) if is_admin(user) else ("owner = ?", [user])

    def _sessions(conn, user: str):
        w, a = _scope(user)
        return conn.execute("SELECT * FROM sessions WHERE %s ORDER BY COALESCE(last_ts, uploaded_at) DESC" % w, a).fetchall()

    def _session_for(conn, pk: int, user: str):
        w, a = _scope(user)
        return conn.execute("SELECT * FROM sessions WHERE id = ? AND %s" % w, [pk] + a).fetchone()

    def ctx(request: Request, user: str, **kw):
        return {"request": request, "user": user, "admin": is_admin(user), **kw}

    def page(request: Request, name: str, user: str, **kw):
        return templates.TemplateResponse(request, name, ctx(request, user, **kw))

    app.state.who, app.state.is_admin, app.state.templates, app.state.page = who, is_admin, templates, page

    @app.exception_handler(Unauthenticated)
    async def _unauth(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "bearer token required"}, status_code=401)
        if login_url:
            return RedirectResponse(login_url + "?next=" + quote(str(request.url.path)), status_code=303)
        return PlainTextResponse("sign-in required", status_code=401)

    # ── pages ──────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, msg: str = ""):
        user = identity(request)
        if not user:
            if anonymous_home:
                return anonymous_home(request)
            raise Unauthenticated()
        with store.db() as conn:
            sessions = _sessions(conn, user)
            w, a = _scope(user)
            totals = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(n_messages),0) AS m,"
                                  " COALESCE(SUM(n_bytes),0) AS b FROM sessions WHERE " + w, a).fetchone()
            used = store.usage(conn, user)
        return page(request, "home.html", user, sessions=sessions, totals=totals, msg=msg,
                    used=used, limit=quota(user), has_local=st.local_projects.is_dir())

    @app.post("/upload")
    async def upload(request: Request, files: list[UploadFile] = File(...)):
        user = who(request)
        added, dup, bad = 0, 0, []
        for f in files:
            if not f.filename:
                continue
            tmp = store.uploads / (".incoming-" + secrets.token_hex(8))
            with tmp.open("wb") as out:
                while chunk := await f.read(1 << 20):
                    out.write(chunk)
            try:
                _, created = store.ingest(tmp, f.filename, user, quota(user))
                added += created
                dup += not created
            except QuotaExceeded as exc:
                bad.append("%s: over your storage limit (%s of %s used)" % (
                    f.filename, human_bytes(exc.used), human_bytes(exc.limit)))
            except Exception as exc:          # a bad file must not sink the batch
                bad.append("%s: %s" % (f.filename, exc))
            finally:
                tmp.unlink(missing_ok=True)
        msg = "indexed %d session(s)" % added + (", %d already present" % dup if dup else "")
        if bad:
            msg += "; failed: " + "; ".join(bad)
        return RedirectResponse("/?msg=" + quote(msg), status_code=303)

    @app.get("/local", response_class=HTMLResponse)
    def local_list(request: Request):
        """Transcripts already on this machine (~/.claude/projects), importable without an upload. Admins only."""
        user = who(request)
        if not is_admin(user):
            return PlainTextResponse("admins only", status_code=403)
        files = []
        root = st.local_projects
        if root.is_dir():
            with store.db() as conn:
                known = {r["sha256"] for r in conn.execute("SELECT sha256 FROM sessions")}
            for p in sorted(root.glob("*/**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
                from datetime import datetime, timezone
                files.append({"path": str(p), "project": p.relative_to(root).parts[0],
                              "name": str(p.relative_to(root)).split("/", 1)[1],
                              "size": p.stat().st_size,
                              "mtime": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                              "indexed": hashlib.sha256(p.read_bytes()).hexdigest() in known if p.stat().st_size < 300 << 20 else None})
        return page(request, "local.html", user, files=files, root=str(root))

    @app.post("/local/import")
    def local_import(request: Request, paths: list[str] = Form(...)):
        user = who(request)
        if not is_admin(user):
            return PlainTextResponse("admins only", status_code=403)
        root = st.local_projects.resolve()
        added = dup = 0
        for raw in paths:
            p = Path(raw).resolve()
            if root not in p.parents or p.suffix != ".jsonl" or not p.is_file():
                continue
            _, created = store.ingest(p, str(p.relative_to(root)), user)
            added += created
            dup += not created
        msg = "indexed %d session(s)" % added + (", %d already present" % dup if dup else "")
        return RedirectResponse("/?msg=" + quote(msg), status_code=303)

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = "", kind: list[str] | None = Query(default=None), session: int | None = None,
               date_from: str = "", date_to: str = "", tool: str = "", limit: int = 100, page_no: int = Query(1, alias="page")):
        user = who(request)
        kinds = [k for k in (kind or []) if k in KINDS]
        match = fts_query(q)
        hits, total = [], 0
        with store.db() as conn:
            sessions = _sessions(conn, user)
            if match:
                where, args = ["messages_fts MATCH ?"], [match]
                if not is_admin(user):
                    where.append("s.owner = ?"); args.append(user)
                if kinds:
                    where.append("m.kind IN (%s)" % ",".join("?" * len(kinds))); args += kinds
                if session:
                    where.append("m.session_pk = ?"); args.append(session)
                if date_from:
                    where.append("m.ts >= ?"); args.append(date_from)
                if date_to:
                    where.append("m.ts < ?"); args.append(next_day(date_to))
                if tool:
                    where.append("m.tool = ?"); args.append(tool)
                sql_from = ("FROM messages_fts f JOIN messages m ON m.id = f.rowid"
                            " JOIN sessions s ON s.id = m.session_pk WHERE " + " AND ".join(where))
                total = conn.execute("SELECT COUNT(*) " + sql_from, args).fetchone()[0]
                limit = max(10, min(limit, 500))
                rows = conn.execute(
                    "SELECT m.id, m.session_pk, m.seq, m.ts, m.kind, m.tool, m.is_error, s.title, s.project,"
                    " snippet(messages_fts, 0, '\x01', '\x02', ' … ', 40) AS snip " + sql_from +
                    " ORDER BY m.ts DESC LIMIT ? OFFSET ?", args + [limit, (page_no - 1) * limit]).fetchall()
                for r in rows:
                    snip = str(escape(r["snip"])).replace("\x01", "<mark>").replace("\x02", "</mark>")
                    hits.append({**dict(r), "snip": Markup(snip)})
        return page(request, "search.html", user, q=q, kinds=kinds, session=session, date_from=date_from,
                    date_to=date_to, tool=tool, hits=hits, total=total, limit=limit, page=page_no,
                    sessions=sessions, pages=(total + limit - 1) // limit if total else 0)

    @app.get("/session/{pk}", response_class=HTMLResponse)
    def session_view(request: Request, pk: int, q: str = "", kind: list[str] | None = Query(default=None),
                     date_from: str = "", date_to: str = "", page_no: int = Query(1, alias="page"),
                     around: int | None = None):
        user = who(request)
        kinds = [k for k in (kind or []) if k in KINDS]
        with store.db() as conn:
            s = _session_for(conn, pk, user)
            if not s:
                return PlainTextResponse("no such session", status_code=404)
            where, args = ["session_pk = ?"], [pk]
            if kinds:
                where.append("kind IN (%s)" % ",".join("?" * len(kinds))); args += kinds
            if date_from:
                where.append("ts >= ?"); args.append(date_from)
            if date_to:
                where.append("ts < ?"); args.append(next_day(date_to))
            cond = " AND ".join(where)
            total = conn.execute("SELECT COUNT(*) FROM messages WHERE " + cond, args).fetchone()[0]
            pages = max(1, (total + PAGE - 1) // PAGE)
            if around is not None:            # a search hit: open the page that holds #m<seq>
                before = conn.execute("SELECT COUNT(*) FROM messages WHERE %s AND seq < ?" % cond,
                                      args + [around]).fetchone()[0]
                page_no = before // PAGE + 1
            page_no = max(1, min(page_no, pages))
            rows = conn.execute("SELECT * FROM messages WHERE %s ORDER BY seq LIMIT ? OFFSET ?" % cond,
                                args + [PAGE, (page_no - 1) * PAGE]).fetchall()
            ids = [r["tool"] for r in rows if r["kind"] == "tool_result" and r["tool"]]
            tool_names = {}
            if ids:
                tool_names = {r["uuid"]: r["tool"] for r in conn.execute(
                    "SELECT uuid, tool FROM messages WHERE session_pk = ? AND kind = 'tool_use' AND uuid IN (%s)"
                    % ",".join("?" * len(ids)), [pk] + ids)}
        msgs = []
        for r in rows:
            text, cut = r["text"], len(r["text"]) > MAX_RENDER
            html = render_text(r["kind"], text[:MAX_RENDER] if cut else text)
            if q:
                html = highlight(html, q)
            msgs.append({**dict(r), "html": html, "cut": cut,
                         "label": tool_names.get(r["tool"], "") if r["kind"] == "tool_result" else r["tool"],
                         "long": len(text) > 1500})
        return page(request, "session.html", user, s=s, msgs=msgs, q=q, kinds=kinds, date_from=date_from,
                    date_to=date_to, page=page_no, pages=pages, total=total, per=PAGE, max_render=MAX_RENDER,
                    first_seq=rows[0]["seq"] if rows else None, last_seq=rows[-1]["seq"] if rows else None)

    @app.get("/session/{pk}/m/{seq}", response_class=HTMLResponse)
    def message_view(request: Request, pk: int, seq: int, q: str = ""):
        """One message in full (used for messages longer than MAX_RENDER)."""
        user = who(request)
        with store.db() as conn:
            s = _session_for(conn, pk, user)
            r = s and conn.execute("SELECT * FROM messages WHERE session_pk = ? AND seq = ?", (pk, seq)).fetchone()
            if not r:
                return PlainTextResponse("no such message", status_code=404)
        html = render_text(r["kind"], r["text"])
        if q:
            html = highlight(html, q)
        return page(request, "message.html", user, s=s, m={**dict(r), "html": html}, q=q)

    @app.get("/session/{pk}/raw")
    def session_raw(request: Request, pk: int):
        with store.db() as conn:
            s = _session_for(conn, pk, who(request))
        if not s:
            return PlainTextResponse("no such session", status_code=404)
        from fastapi.responses import FileResponse
        return FileResponse(store.uploads / f"{s['sha256']}.jsonl", media_type="application/x-ndjson",
                            filename=s["filename"])

    @app.get("/session/{pk}/text")
    def session_text(request: Request, pk: int):
        """The whole session as plain text — for grep, or for pasting elsewhere."""
        with store.db() as conn:
            s = _session_for(conn, pk, who(request))
            if not s:
                return PlainTextResponse("no such session", status_code=404)
            rows = conn.execute("SELECT ts, kind, tool, text FROM messages WHERE session_pk = ? ORDER BY seq", (pk,))
            out = ["# %s" % s["title"], ""]
            for r in rows:
                head = "## [%s] %s" % (fmt_ts(r["ts"]), r["kind"])
                if r["tool"] and r["kind"] == "tool_use":
                    head += " " + r["tool"]
                out += [head, r["text"], ""]
        return PlainTextResponse("\n".join(out))

    @app.post("/session/{pk}/delete")
    def session_delete(request: Request, pk: int):
        with store.db() as conn:
            s = _session_for(conn, pk, who(request))
            if s:
                store.delete_session(conn, s)
        return RedirectResponse("/?msg=deleted", status_code=303)

    # ── per-user API tokens + the upload API the Claude Code hook talks to ──

    def _token_hash(tok: str) -> str:
        return hashlib.sha256(tok.encode()).hexdigest()

    @app.get("/token", response_class=HTMLResponse)
    def token_page(request: Request):
        user = who(request)
        with store.db() as conn:
            row = conn.execute("SELECT token_hash, created_at FROM users WHERE name = ?", (user,)).fetchone()
        return page(request, "token.html", user, has_token=bool(row and row["token_hash"]),
                    created=row["created_at"] if row else None, new="", base=str(request.base_url).rstrip("/"))

    @app.post("/token/rotate")
    def token_rotate(request: Request):
        user = who(request)
        tok = "cs_" + secrets.token_urlsafe(30)
        with store.db() as conn:
            conn.execute("INSERT INTO users (name, token_hash, created_at) VALUES (?,?,?)"
                         " ON CONFLICT(name) DO UPDATE SET token_hash = excluded.token_hash, created_at = excluded.created_at",
                         (user, _token_hash(tok), now_iso()))
        # Shown once, on the page that follows; never stored in clear.
        return page(request, "token.html", user, has_token=True, new=tok, created=now_iso(),
                    base=str(request.base_url).rstrip("/"))

    def _api_user(request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        tok = auth[7:].strip()
        if not tok:
            return None
        with store.db() as conn:
            row = conn.execute("SELECT name FROM users WHERE token_hash = ?", (_token_hash(tok),)).fetchone()
        return row["name"] if row else None

    app.state.api_user = _api_user

    @app.post("/api/upload")
    async def api_upload(request: Request, name: str = ""):
        """Raw transcript in the body (optionally Content-Encoding: gzip), owner =
        the bearer token's user. Idempotent per (user, sessionId): a newer copy of
        a session replaces the one held. This is what the Stop/SessionEnd hook calls."""
        user = _api_user(request)
        if not user:
            return JSONResponse({"error": "bearer token required"}, status_code=401)
        tmp = store.uploads / (".api-" + secrets.token_hex(8))
        gz = request.headers.get("content-encoding", "").lower() == "gzip"
        try:
            with tmp.open("wb") as out:
                if gz:
                    import zlib
                    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    async for chunk in request.stream():
                        out.write(d.decompress(chunk))
                    out.write(d.flush())
                else:
                    async for chunk in request.stream():
                        out.write(chunk)
            if tmp.stat().st_size == 0:
                return JSONResponse({"error": "empty body"}, status_code=400)
            pk, created = store.ingest(tmp, name or "api-upload.jsonl", user, quota(user))
        except QuotaExceeded as exc:
            return JSONResponse({"error": "storage quota exceeded", "used": exc.used, "limit": exc.limit,
                                 "incoming": exc.incoming, "url": "/account"}, status_code=413)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        finally:
            tmp.unlink(missing_ok=True)
        with store.db() as conn:
            s = conn.execute("SELECT session_id, n_messages FROM sessions WHERE id = ?", (pk,)).fetchone()
        return {"ok": True, "user": user, "session_id": s["session_id"], "messages": s["n_messages"],
                "status": "indexed" if created else "unchanged", "url": "/session/%d" % pk}

    @app.get("/api/whoami")
    def api_whoami(request: Request):
        user = _api_user(request)
        if not user:
            return JSONResponse({"error": "bearer token required"}, status_code=401)
        with store.db() as conn:
            used = store.usage(conn, user)
        return {"user": user, "used": used, "limit": quota(user)}

    @app.get("/hook/" + HOOK_NAME)
    def hook_script():
        """The client-side hook script, served so a new machine needs only curl."""
        return PlainTextResponse((HERE / "hook" / HOOK_NAME).read_text(), media_type="text/x-shellscript")

    @app.get("/healthz")
    def healthz():
        with store.db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        return {"status": "ok", "sessions": n}

    return app

