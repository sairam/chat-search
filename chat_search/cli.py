"""`chat-search` command: run the server, import transcripts, push to a server."""
import argparse
import os
import sys
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="chat-search",
                                 description="Render and full-text search Claude Code session transcripts.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web app (default: http://127.0.0.1:9180)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=9180)
    s.add_argument("--data", default=os.environ.get("CHAT_SEARCH_DATA", ""),
                   help="data directory (default ~/.local/share/chat-search)")
    s.add_argument("--auth", choices=("none", "proxy"), default=os.environ.get("CHAT_SEARCH_AUTH", "none"),
                   help="none: single local user; proxy: trust X-Remote-User from your reverse proxy")
    s.add_argument("--projects", default="", help="Claude Code projects dir to import from (default ~/.claude/projects)")
    s.add_argument("--open", action="store_true", help="open the browser")

    i = sub.add_parser("import", help="index transcript files straight into the data directory")
    i.add_argument("paths", nargs="+", help=".jsonl files or directories (searched recursively)")
    i.add_argument("--data", default=os.environ.get("CHAT_SEARCH_DATA", ""))
    i.add_argument("--owner", default="local")

    p = sub.add_parser("push", help="upload transcripts to a chat-search server (uses ~/.config/chat-search/config)")
    p.add_argument("paths", nargs="*", help="files to push; none = --check")

    h = sub.add_parser("hook", help="print the Claude Code hook script (install to ~/.local/bin/chat-search-push)")

    a = ap.parse_args(argv)
    if a.cmd == "serve":
        for k, v in (("CHAT_SEARCH_DATA", a.data), ("CHAT_SEARCH_AUTH", a.auth), ("CLAUDE_PROJECTS_DIR", a.projects)):
            if v:
                os.environ[k] = v
        import uvicorn
        from .app import Settings, create_app
        app = create_app(Settings())
        url = "http://%s:%d/" % (a.host, a.port)
        print("chat-search on", url, "· data:", app.state.settings.data_dir, file=sys.stderr)
        if a.open:
            import threading, webbrowser
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    elif a.cmd == "import":
        from .app import Settings
        from .store import Store
        if a.data:
            os.environ["CHAT_SEARCH_DATA"] = a.data
        store = Store(Settings().data_dir)
        store.init()
        n = 0
        for raw in a.paths:
            path = Path(raw)
            files = sorted(path.rglob("*.jsonl")) if path.is_dir() else [path]
            for f in files:
                pk, created = store.ingest(f, f.name, a.owner)
                n += created
                print("%s %s" % ("indexed " if created else "unchanged", f))
        print("%d session(s) indexed into %s" % (n, store.data_dir))
    elif a.cmd == "push":
        import subprocess
        script = Path(__file__).parent / "hook" / "chat-search-push"
        sys.exit(subprocess.call([str(script)] + (a.paths or ["--check"])))
    elif a.cmd == "hook":
        sys.stdout.write((Path(__file__).parent / "hook" / "chat-search-push").read_text())


if __name__ == "__main__":
    main()
