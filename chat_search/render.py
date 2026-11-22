"""Rendering and query helpers shared by the routes."""
import re
from datetime import datetime, timedelta

from markdown_it import MarkdownIt
from markupsafe import Markup, escape

md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


def render_text(kind: str, text: str) -> Markup:
    if kind in ("human", "assistant", "summary"):
        return Markup(md.render(text))
    return Markup("<pre>%s</pre>" % escape(text))


def fts_terms(q: str) -> list[str]:
    """Plain-text query → list of terms; a "quoted phrase" stays one term."""
    return [m.group(1) or m.group(2) for m in re.finditer(r'"([^"]+)"|(\S+)', q or "")]


def fts_query(q: str) -> str:
    """Every term is quoted so FTS5 syntax never leaks in; terms are ANDed.
    A trailing * on a term keeps prefix matching."""
    out = []
    for t in fts_terms(q):
        prefix = t.endswith("*")
        t = t.rstrip("*").replace('"', '""')
        if not t:
            continue
        out.append('"%s"%s' % (t, "*" if prefix else ""))
    return " ".join(out)


def highlight(html: Markup, q: str) -> Markup:
    """Wrap each search term in <mark>, outside of tags."""
    terms = [t.rstrip("*") for t in fts_terms(q) if t.rstrip("*")]
    if not terms:
        return html
    pat = re.compile("(" + "|".join(re.escape(t) for t in terms) + ")", re.IGNORECASE)
    parts = re.split(r"(<[^>]+>)", str(html))
    return Markup("".join(p if p.startswith("<") else pat.sub(r"<mark>\1</mark>", p) for p in parts))


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def human_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f %s" % (n, unit) if unit == "B" else ("%.1f" % n).rstrip("0").rstrip(".") + " " + unit
        n /= 1024
    return str(n)


def next_day(d: str) -> str:
    try:
        return (datetime.fromisoformat(d) + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return d


# ── chat view (Settings.ui == "chat") ──────────────────────────────────────
# The transcript stores harness traffic as "human" records too: task
# notifications, reminders, slash-command echoes. In the chat view only what a
# person typed gets a bubble; the rest becomes a quiet one-line note.
COMPACT_NOTE = "Summary of the earlier conversation"   # written by Claude Code at each compaction
_NOTES = (
    ("<task-notification>", "Background task update"),
    ("<system-reminder>", "System reminder"),
    ("<local-command-caveat>", "Local command"),
    ("<local-command-stdout>", "Command output"),
    ("<local-command-stderr>", "Command output"),
    ("<command-name>", "Slash command"),
    ("<command-message>", "Slash command"),
    ("<fork-boilerplate>", "Fork instructions"),
    ("<bash-input>", "Shell command"),
    ("<bash-stdout>", "Shell output"),
    ("Another Claude session sent a message:", "Message from another session"),
    ("The coordinator sent a message", "Message from the coordinator"),
    ("This session is being continued from a previous", COMPACT_NOTE),
    ("Base directory for this skill:", "Skill instructions"),
    ("Caveat: The messages below", "Local command"),
    ("[Request interrupted by user", "Interrupted by the user"),
    ("[Image", "Image"),
    ("[image]", "Image"),
)
_SUMMARY_KEYS = ("file_path", "path", "pattern", "query", "url", "description", "skill", "subject", "prompt", "message")


def _clip(s: str, n: int = 150) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def human_note(text: str) -> str | None:
    """Label for a "human" record that the harness wrote, or None for a real prompt."""
    t = text.lstrip()
    for prefix, label in _NOTES:
        if t.startswith(prefix):
            if prefix == "<command-name>":
                m = re.match(r"<command-name>\s*(/?[\w:.-]+)", t)
                return "Ran %s" % m.group(1) if m else label
            return label
    return None


def tool_summary(tool: str | None, text: str) -> str:
    """One line that says what a tool call did: a Bash call's description, a
    file tool's path, else the first line of its input."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return ""
    if tool == "Bash":
        for l in reversed(lines[-2:]):
            if l.startswith("# "):
                return _clip(l[2:])
        return _clip(lines[0])
    for key in _SUMMARY_KEYS:
        for l in lines:
            if l.startswith(key + ":"):
                v = l[len(key) + 1:].strip()
                if v:
                    return _clip(v)
    return _clip(lines[0])


def chat_items(msgs: list[dict]) -> list[dict]:
    """Group a page of messages for the chat view: prompts and replies stay
    whole; thinking and tool calls between them become rows of one "steps"
    block, each call carrying its result when that is on the same page."""
    items, rows, calls = [], None, {}

    def step(row):
        nonlocal rows
        if rows is None:
            rows = []
            items.append({"type": "steps", "rows": rows, "m": row["m"]})
        rows.append(row)

    for m in msgs:
        kind, hit = m["kind"], "<mark>" in m["html"]
        if kind == "tool_use":
            row = {"type": "tool", "m": m, "result": None, "hit": hit,
                   "summary": tool_summary(m["tool"], m["text"])}
            if m["uuid"]:
                calls[m["uuid"]] = row
            step(row)
        elif kind == "tool_result":
            row = calls.get(m["tool"])
            if row and row["result"] is None:
                row["result"], row["hit"] = m, row["hit"] or hit
            else:                               # its call is on an earlier page, or filtered out
                step({"type": "result", "m": m, "hit": hit, "summary": _clip(m["text"])})
        elif kind == "thinking":
            step({"type": "thinking", "m": m, "hit": hit, "summary": _clip(m["text"])})
        elif kind == "system" and not m["text"].strip():
            items.append({"type": "anchor", "m": m})   # bookkeeping record: keep only its #m anchor
        else:
            rows = None
            if kind == "human":
                note = human_note(m["text"])
                if note:
                    short = len(m["text"]) < 140 and "\n" not in m["text"].strip()
                    items.append({"type": "note", "m": m, "label": note, "hit": hit, "short": short,
                                  "summary": "" if short else _clip(re.sub(r"<[^>]+>", " ", m["text"]), 110)})
                else:
                    items.append({"type": "human", "m": m})
            elif kind == "assistant":
                items.append({"type": "assistant", "m": m})
            else:                               # system, summary
                label = (m["tool"] or kind).replace("_", " ")
                items.append({"type": "note", "m": m, "label": label[:1].upper() + label[1:], "hit": hit,
                              "short": False, "summary": _clip(m["text"], 110)})
    return items


def first_line(text: str, n: int = 140) -> str:
    return _clip(next((l for l in text.splitlines() if l.strip()), ""), n)


def toc_entries(rows, hits: set) -> list[dict]:
    """Contents of a whole session: one entry per prompt a person typed, with
    what its turn held (tool calls, an error, search hits). A prompt that
    recurs (a scheduled task firing) folds into one entry per unbroken run.
    rows: seq, ts, kind, tool, is_error, head (start of the text)."""
    entries, cur = [], None
    for r in rows:
        kind = r["kind"]
        if kind == "human" and human_note(r["head"]) is None:
            cur = {"seq": r["seq"], "ts": r["ts"], "text": first_line(r["head"]), "steps": 0, "err": 0, "hits": 0, "n": 1}
            entries.append(cur)
        elif kind == "system" and r["tool"] == "compact_boundary":
            entries.append({"seq": r["seq"], "ts": r["ts"], "mile": "Conversation compacted"})
        if cur is not None:
            cur["steps"] += kind == "tool_use"
            cur["err"] |= int(bool(r["is_error"]))
            cur["hits"] += r["seq"] in hits
    seen = {}
    for e in entries:
        if "mile" not in e:
            seen[e["text"][:60]] = seen.get(e["text"][:60], 0) + 1
    out = []
    for e in entries:
        prev = out[-1] if out else None
        if (prev and "mile" not in e and "mile" not in prev and seen[e["text"][:60]] >= 3
                and prev["text"][:60] == e["text"][:60]):
            prev["n"] += 1
            for k in ("steps", "hits"):
                prev[k] += e[k]
            prev["err"] |= e["err"]
            prev["end"] = e["seq"]
        else:
            out.append(e)
    return out
