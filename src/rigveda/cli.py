"""Command-line interface for Rigveda AI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config as configuration
from .indexer import index_paths
from .llm import OllamaError, ask
from .search import cite, search
from .store import connect


def folders_command(args, config: configuration.Config) -> None:
    if args.action == "list":
        print("\n".join(config.folders) if config.folders else "No folders yet. Add one with `rigveda folders add PATH`.")
        return
    for raw in args.paths:
        folder = str(Path(raw).expanduser().resolve())
        if args.action == "add":
            if not Path(folder).is_dir():
                raise SystemExit(f"Not a folder: {folder}")
            if folder not in config.folders:
                config.folders.append(folder)
        elif folder in config.folders:
            config.folders.remove(folder)
    path = configuration.save(config)
    print(f"Saved {path}. Watched folders:\n" + ("\n".join(config.folders) or "(none)"))
    if restart_service():
        print("Restarted the background watcher (rigveda.service) with the new folders.")
    elif args.action == "remove":
        print("Run `rigveda index` to drop their files from the index.")


def restart_service() -> bool:
    """Restart the systemd user service, if installed, so it watches the updated folders."""
    import shutil
    import subprocess

    if not shutil.which("systemctl"):
        return False
    enabled = subprocess.run(["systemctl", "--user", "is-enabled", "rigveda.service"], capture_output=True, text=True)
    if enabled.stdout.strip() != "enabled":
        return False
    return subprocess.run(["systemctl", "--user", "restart", "rigveda.service"], capture_output=True).returncode == 0


def status_command(db: Path, config: configuration.Config) -> None:
    con = connect(db)
    rows = con.execute("SELECT kind, COUNT(*) files FROM documents GROUP BY kind ORDER BY files DESC").fetchall()
    chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    con.close()
    print(f"Database: {db}\nFolders: {', '.join(config.folders) or '(none)'}")
    kinds = ", ".join(f"{row['files']} {row['kind']}" for row in rows) or "none"
    print(f"Files: {sum(row['files'] for row in rows)} ({kinds}); chunks: {chunks}")


def facts_command(args, db: Path, config: configuration.Config) -> None:
    from . import facts

    if args.action == "owner":
        if args.items:
            config.owner_name = " ".join(args.items)
            configuration.save(config)
            print(f"Personal facts will be collected for {config.owner_name}. Run `rigveda facts scan --rescan` to look again.")
        else:
            print(config.owner_name or "Not set. Set it with `rigveda facts owner \"Your Full Name\"`.")
        return
    con = connect(db)
    try:
        if args.action == "scan":
            if not config.owner_name:
                print("Tip: set your name with `rigveda facts owner \"Your Full Name\"` so only your own details are collected.")
            scanned = facts.scan(con, config, lambda message: print(message, file=sys.stderr), rescan=args.rescan)
            pending = len(facts.listing(con, "pending"))
            print(f"Scanned {scanned} document(s). {pending} new fact(s), already suggested in forms. Check or reject them with "
                  "`rigveda facts list` or the review page at http://127.0.0.1:8765/facts (run `rigveda serve`).")
        elif args.action == "list":
            status = None if args.status == "all" else args.status
            rows = facts.listing(con, status)
            if not rows:
                print(f"No {args.status} facts." if status else "No facts yet. Run `rigveda facts scan`.")
            for fact in rows:
                value = facts.mask(fact["value"]) if fact["sensitive"] and not args.show else fact["value"]
                seen = f", in {fact['files']} files" if fact["files"] > 1 else ""
                print(f"[{fact['id']}] {fact['label']}: {value}  ({fact['status']}{seen})")
                for source in fact["sources"][:3]:
                    where = f" ({source['location']})" if source["location"] else ""
                    print(f"      {source['path']}{where}: \"{source['evidence'][:120]}\"")
        elif args.action == "add":
            if len(args.items) < 2:
                raise SystemExit("Usage: rigveda facts add KEY VALUE. Keys: " + ", ".join(facts.KEYS))
            fact_id = facts.add_manual(con, args.items[0], " ".join(args.items[1:]))
            print(f"Added and approved fact {fact_id}.")
        else:  # approve, reject
            status = {"approve": "approved", "reject": "rejected"}[args.action]
            if not args.items:
                raise SystemExit(f"Usage: rigveda facts {args.action} ID [ID ...]")
            for item in args.items:
                facts.decide(con, int(item), status)
            print(f"Marked {len(args.items)} fact(s) {status}.")
    except (KeyError, ValueError) as error:
        raise SystemExit(f"{error}") from error
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Private, local-first filesystem assistant.")
    parser.add_argument("--db", help="Index location (default: from config, ~/.local/share/rigveda/rigveda.db)")
    commands = parser.add_subparsers(dest="command", required=True)

    cmd_folders = commands.add_parser("folders", help="Choose which folders Rigveda knows about.")
    cmd_folders.add_argument("action", choices=["add", "remove", "list"])
    cmd_folders.add_argument("paths", nargs="*")
    cmd_index = commands.add_parser("index", help="Index the watched folders, or the paths given.")
    cmd_index.add_argument("paths", nargs="*")
    cmd_watch = commands.add_parser("watch", help="Index the watched folders (or the paths given), then keep the index updated as files change.")
    cmd_watch.add_argument("paths", nargs="*")
    commands.add_parser("status", help="Show what is indexed.")
    cmd_facts = commands.add_parser("facts", help="Your personal facts profile, used to fill forms.",
        description="scan: look for personal facts in indexed documents; list: show them; approve/reject IDs; "
                    "add KEY VALUE: type one in; owner NAME: whose facts to collect. Review in the browser at /facts.")
    cmd_facts.add_argument("action", choices=["scan", "list", "approve", "reject", "add", "owner"])
    cmd_facts.add_argument("items", nargs="*")
    cmd_facts.add_argument("--status", choices=["pending", "approved", "rejected", "all"], default="all")
    cmd_facts.add_argument("--show", action="store_true", help="Show ID numbers in full")
    cmd_facts.add_argument("--rescan", action="store_true", help="Scan every document again")
    cmd_token = commands.add_parser("token", help="Show the secret the browser extension needs.")
    cmd_token.add_argument("--new", action="store_true", help="Replace it (the old one stops working)")
    cmd_search = commands.add_parser("search", help="Search indexed files.")
    cmd_search.add_argument("question"); cmd_search.add_argument("--limit", type=int, default=5)
    cmd_search.add_argument("--keywords-only", action="store_true", help="Skip meaning search")
    cmd_ask = commands.add_parser("ask", help="Ask a local Ollama model about indexed files.")
    cmd_ask.add_argument("question"); cmd_ask.add_argument("--limit", type=int, default=12)
    cmd_ask.add_argument("--model"); cmd_ask.add_argument("--ollama")
    cmd_write = commands.add_parser("write", help="Draft text from your files. Without TEXT, open the writing window.")
    cmd_write.add_argument("text", nargs="?", help="What to write, e.g. \"a short bio for a conference\"")
    cmd_write.add_argument("--port", type=int, default=8765, help="Port of `rigveda serve`, for the window")
    cmd_write.add_argument("--model"); cmd_write.add_argument("--ollama")
    cmd_hotkey = commands.add_parser("hotkey", help="A desktop keyboard shortcut (GNOME) that opens the writing window.")
    cmd_hotkey.add_argument("--keys", default="<Super><Alt>r", help="Shortcut in GNOME's notation (default: <Super><Alt>r)")
    cmd_hotkey.add_argument("--remove", action="store_true", help="Remove the shortcut")
    cmd_serve = commands.add_parser("serve", help="Run the local browser chat interface.")
    cmd_serve.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    cmd_serve.add_argument("--port", type=int, default=8765)
    cmd_serve.add_argument("--limit", type=int, default=12)
    cmd_serve.add_argument("--model"); cmd_serve.add_argument("--ollama")
    args = parser.parse_args()

    config = configuration.load()
    if getattr(args, "model", None):
        config.model = args.model
    if getattr(args, "ollama", None):
        config.ollama = args.ollama
    db = Path(args.db).expanduser().resolve() if args.db else config.database_path

    if args.command == "folders":
        if args.action != "list" and not args.paths:
            parser.error(f"folders {args.action} needs at least one PATH")
        folders_command(args, config)
        return
    if args.command == "token":
        token = configuration.load_token(renew=args.new)
        print(token)
        print(f"Paste this into the Rigveda AI extension's options. Stored in {configuration.token_path()}"
              + (". Restart `rigveda serve` to use the new token." if args.new else "."), file=sys.stderr)
        return
    if args.command == "hotkey":
        from . import desktop
        if args.remove:
            print("Shortcut removed." if desktop.remove_hotkey() else "There was no Rigveda shortcut.")
            return
        import shutil
        program = shutil.which("rigveda")
        command = f"{program} write" if program else f"{sys.executable} -m rigveda write"
        desktop.install_hotkey(command, args.keys)
        keys = "+".join(part.title() for part in args.keys.replace("><", "+").strip("<>").replace(">", "+").split("+"))
        print(f"Press {keys} anywhere to open the writing window. "
              "Change or remove it in Settings → Keyboard → Custom Shortcuts, or with `rigveda hotkey --remove`.")
        return
    if args.command == "write":
        if not args.text:
            from .desktop import open_window
            open_window(args.port)
            return
        from .write import write
        try:
            print(write(db, config, {"instruction": args.text})["text"])
        except OllamaError as error:
            raise SystemExit(str(error))
        return
    if args.command == "facts":
        facts_command(args, db, config)
        return
    if args.command == "status":
        status_command(db, config)
        return
    if args.command in {"index", "watch"}:
        paths = getattr(args, "paths", None) or config.folders
        if not paths:
            message = "No folders to index. Add one with `rigveda folders add PATH`, or pass paths."
            if args.command == "watch":  # exit cleanly so the background service doesn't restart in a loop
                print(message, file=sys.stderr)
                return
            raise SystemExit(message)
        if args.command == "watch":
            from .watch import watch
            watch(db, paths, config)
            return
        print(f"Index complete: {index_paths(db, paths, config)}. Database: {db}")
        return
    if args.command == "serve":
        if args.host not in {"127.0.0.1", "localhost", "::1"}:
            print("Warning: this server has no authentication. Do not expose it to an untrusted network.", file=sys.stderr)
        from .server import ServerConfig, run_server
        token = configuration.load_token()
        run_server(ServerConfig(db, config.model, config.ollama, args.limit, config, token), args.host, args.port)
        return

    sources = search(db, args.question, args.limit, None if getattr(args, "keywords_only", False) else config)
    if not sources:
        print("No matching indexed text. Run `rigveda index` first."); return
    if args.command == "search":
        for row in sources:
            print(f"{cite(row)}\n  {row['text'].replace(chr(10), ' ')[:320]}\n")
        return
    try:
        print(ask(args.question, sources, config.model, config.ollama))
    except OllamaError as error:
        print(error, file=sys.stderr)
        print("Search still works: `rigveda search \"your question\"`", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
