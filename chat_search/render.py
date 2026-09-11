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
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024
    return str(n)


def next_day(d: str) -> str:
    try:
        return (datetime.fromisoformat(d) + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return d
