# chat-search

Render and full-text search your **Claude Code** session transcripts.

Claude Code writes every session to `~/.claude/projects/<project>/<session-id>.jsonl`.
Those files hold everything — your prompts, Claude's replies and thinking, every
tool call and its output — but there is no way to read or search them. chat-search
gives you:

* a readable HTML view of each session (markdown, tool calls, results, subagents),
  paged so 100 MB sessions open instantly;
* full-text search across all sessions (SQLite FTS5) with filters by message kind,
  date, tool and session, and highlighted hits that jump to the exact message;
* a **Claude Code hook** that uploads sessions automatically when a turn or a
  session ends, so the archive is always current;
* plain-text and raw-JSONL export per session.

Everything lives in one directory: a SQLite file and the raw transcripts.

> Hosted version: [chat-search.bitgeek.in](https://chat-search.bitgeek.in) — same
> app, nothing to run, free up to 100 MB of transcripts.

## Run it locally

```bash
pipx install git+https://github.com/sairam/chat-search   # PyPI release coming
chat-search serve --open        # http://127.0.0.1:9180
```

Then either **Upload** transcripts on the home page, use **local transcripts**
to pick from `~/.claude/projects`, or import from the shell:

```bash
chat-search import ~/.claude/projects
```

Data goes to `~/.local/share/chat-search` (`--data DIR` or `CHAT_SEARCH_DATA`).

Docker:

```bash
docker run -d -p 127.0.0.1:9180:9180 -v chat-search:/data -v ~/.claude/projects:/projects:ro \
  -e CLAUDE_PROJECTS_DIR=/projects ghcr.io/sairam/chat-search
```

## Automatic upload from Claude Code (hook)

Works with a local server, a team server, or the hosted service — the hook only
needs a URL and a token.

1. Open **hook / token** in the app and create a token.
2. On the machine running Claude Code:

   ```bash
   curl -fsS http://127.0.0.1:9180/hook/chat-search-push -o ~/.local/bin/chat-search-push
   chmod +x ~/.local/bin/chat-search-push
   mkdir -p ~/.config/chat-search
   printf 'CHAT_SEARCH_URL=%s\nCHAT_SEARCH_TOKEN=%s\n' http://127.0.0.1:9180 cs_… > ~/.config/chat-search/config
   chmod 600 ~/.config/chat-search/config
   chat-search-push --check
   ```

3. Add to `~/.claude/settings.json` (merge into an existing `hooks` block):

   ```json
   {
     "hooks": {
       "Stop":       [{ "hooks": [{ "type": "command", "command": "~/.local/bin/chat-search-push" }] }],
       "SessionEnd": [{ "hooks": [{ "type": "command", "command": "~/.local/bin/chat-search-push" }] }]
     }
   }
   ```

The hook reads `transcript_path` from the hook's stdin, gzips the transcript
(and any subagent transcripts beside it) and POSTs it to `/api/upload` in the
background — Claude never waits on it. `Stop` pushes are throttled to one per
session per 10 minutes (`CHAT_SEARCH_THROTTLE`); `SessionEnd` always pushes.
Re-pushing a session replaces the copy on the server. Log:
`~/.cache/chat-search/push.log`. Manual push: `chat-search-push FILE...`.

## Team install (shared server)

Put the app behind a reverse proxy that authenticates users and start it with
`--auth proxy` (or `CHAT_SEARCH_AUTH=proxy`). The proxy must set `X-Remote-User`
on every request (and strip it from clients); each user sees only their own
uploads, users listed in `CHAT_SEARCH_ADMINS` see everything. Leave `/api/*`
and `/hook/*` outside the proxy's login — `/api/*` is protected by the bearer
token. Caddy example:

```caddy
chat-search.example.com {
    @open path /api/* /hook/*
    handle @open {
        reverse_proxy 127.0.0.1:9180 { header_up -X-Remote-User }
    }
    handle {
        basic_auth { alice <bcrypt-hash> }
        reverse_proxy 127.0.0.1:9180 { header_up X-Remote-User {http.auth.user.id} }
    }
}
```

## Important: transcripts contain what was on screen

A session transcript includes every command output Claude saw — if a secret was
ever printed in a session, it is in the transcript. Keep the server private
(loopback, VPN, or behind authentication), and treat a transcript archive with
the care you give a password manager.

## API

* `POST /api/upload?name=<file.jsonl>` — bearer token; raw body, optional
  `Content-Encoding: gzip`; returns `{ok, user, session_id, messages, status, url}`.
  `413` with `{error, used, limit}` when over a storage quota.
* `GET /api/whoami` — `{user, used, limit}`.
* `GET /healthz`.

## Embedding

```python
from chat_search import Settings, create_app
app = create_app(Settings(data_dir="/srv/cs"),
                 identity=lambda request: ...,   # -> user name or None
                 quota=lambda user: 100 << 20)    # bytes, or None
```

## Development

```bash
uv venv && uv pip install -e . pytest && chat-search serve --data /tmp/cs
.venv/bin/python -m pytest
```

MIT © Sairam Kunala
