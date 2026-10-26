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


def drive_command(args, db: Path, config: configuration.Config) -> None:
    from . import drive_index, gdrive
    try:
        if args.action == "setup":
            if not args.file:
                raise SystemExit("Give the key you downloaded: rigveda drive setup ~/Downloads/client_secret_….json")
            print(f"Moved your Google key to {gdrive.setup(Path(args.file))} (readable only by you), so it isn't left "
                  "in Downloads to be indexed. Next: rigveda drive connect")
        elif args.action == "connect":
            print("Opening Google sign-in in your browser…")
            about = gdrive.connect()
            print(f"Connected to {about['email']} (read-only). Next: rigveda drive survey")
        elif args.action == "survey":
            about = gdrive.account()
            print(f"Looking through {about['email']}'s Drive (names, types and sizes only; nothing is downloaded)…", file=sys.stderr)
            report = gdrive.survey(gdrive.files(lambda seen: print(f"\r  {seen:,} items listed", end="", file=sys.stderr)))
            print(file=sys.stderr)
            used = f"{gdrive.size(about['used'])} used" + (f" of {gdrive.size(about['limit'])}" if about["limit"] else "")
            print(f"\n{report['files']:,} files in {report['folders']:,} folders · {used}\n")
            print(f"{'':42}{'files':>9}{'size':>11}{'shared':>9}   indexing")
            for name, group in report["groups"].items():
                if group["files"]:
                    print(f"{name:42}{group['files']:>9,}{gdrive.size(group['bytes']):>11}{group['shared']:>9,}   {group['handling']}")
            print(f"\nRigveda could read {report['readable_files']:,} files ({gdrive.size(report['readable_bytes'])} "
                  "plus Google Docs, which Google doesn't count). Sizes are what's stored; shared files don't use your quota.")
        elif args.action == "index":
            if args.background:
                import shutil
                import subprocess
                program = shutil.which("rigveda") or sys.argv[0]
                command = [program, "drive", "index"] + (["--only", args.only] if args.only else [])
                result = subprocess.run(["systemd-run", "--user", "--unit=rigveda-drive-index", "--collect",
                                         "--nice=10", "--description=Rigveda AI: index Google Drive", *command],
                                        capture_output=True, text=True)
                if result.returncode:
                    raise SystemExit(f"Couldn't start it in the background: {result.stderr.strip()}")
                print("Indexing Google Drive in the background. Progress: rigveda drive status · "
                      "log: journalctl --user -u rigveda-drive-index -f · stop: systemctl --user stop rigveda-drive-index "
                      "(it carries on from where it stopped next time)")
                return
            con = connect(db)
            try:
                log = lambda message: print(message, file=sys.stderr, flush=True)  # noqa: E731
                listed = drive_index.refresh(con, log)
                log(f"Drive has {listed['files']:,} files: {listed['added']:,} new, {listed['changed']:,} changed, "
                    f"{listed['removed']:,} removed since last time.")
                stages = {"documents": {0}, "other": {1}, "media": {2}, "images": {3}}
                only = set().union(*(stages[name] for name in args.only.split(","))) if args.only else None
                counts = drive_index.run(con, config, log, only)
                log("Drive: done · " + (", ".join(f"{count:,} {name}" for name, count in sorted(counts.items())) or "nothing new"))
                if config.scan_facts:
                    from . import facts
                    facts.scan(con, config, log)
            finally:
                con.close()
        elif args.action == "retry":
            con = connect(db)
            with con:
                count = con.execute("UPDATE drive_files SET state='pending', note='' WHERE state='failed'").rowcount
            con.close()
            print(f"{count:,} failed file(s) will be tried again on the next `rigveda drive index`.")
        elif args.action == "status":
            state = gdrive.status()
            print(f"Connected to {state['email']} (read-only)." if state else
                  "Not connected." + ("" if gdrive.client_path().exists() else " Start with docs/GOOGLE_DRIVE.md."))
            con = connect(db)
            report = drive_index.progress(con)
            if report["files"]:
                print(f"\n{report['files']:,} Drive files known · " + ", ".join(
                    f"{count:,} {name}" for name, count in sorted(report["states"].items())))
                for entry in report["stages"]:
                    if entry["files"]:
                        print(f"  {entry['name']:28} {entry['done']:>8,} / {entry['files']:,} done")
                failed = con.execute("SELECT note, COUNT(*) AS n FROM drive_files WHERE state IN ('failed', 'waiting') "
                                     "GROUP BY note ORDER BY n DESC LIMIT 5").fetchall()
                for row in failed:
                    print(f"  {row['n']:>6,} × {row['note'][:100]}")
            con.close()
        else:  # disconnect
            print("Disconnected: Rigveda's access is revoked at Google and the sign-in is deleted."
                  if gdrive.disconnect() else "Rigveda wasn't connected to Google Drive.")
    except gdrive.DriveError as error:
        raise SystemExit(str(error))


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
    cmd_drive = commands.add_parser("drive", help="Google Drive, read-only: setup, connect, survey, index, status, retry, disconnect.",
        description="setup FILE.json: store your Google key; connect: sign in; survey: see what's in your Drive by type and size "
                    "(nothing is downloaded); index: read Drive into the index without keeping copies; status: connection and "
                    "progress; retry: try failed files again; disconnect: revoke access. See docs/GOOGLE_DRIVE.md.")
    cmd_drive.add_argument("action", choices=["setup", "connect", "survey", "index", "status", "retry", "disconnect"])
    cmd_drive.add_argument("file", nargs="?", help="for setup: the client_secret_….json you downloaded")
    cmd_drive.add_argument("--only", help="for index: documents, other, media, images (comma-separated)")
    cmd_drive.add_argument("--background", action="store_true", help="for index: keep running after you close the terminal")
    cmd_listen = commands.add_parser("listen", help='Voice commands: say "Rigved", then e.g. "open Downloads"; a sage answers.')
    cmd_listen.add_argument("--text", help='Do one command as if spoken, e.g. "open chrome and open fidelity"')
    cmd_listen.add_argument("--dry-run", action="store_true", help="With --text: say what would happen, without doing it")
    cmd_listen.add_argument("--voices", action="store_true", help="Hear each voice, to choose one with --voice")
    cmd_listen.add_argument("--voice", help="Who speaks, e.g. am_michael or hm_omega (remembered)")
    cmd_listen.add_argument("--persona", choices=["witty", "sage"], help="How Rigved talks (remembered)")
    cmd_listen.add_argument("--mode", choices=["key", "wake"],
                            help='key: listen only while Right Ctrl is held or after Super+R; wake: the keys, and also '
                                 'always listen for "Hey Rigved" (remembered)')
    cmd_listen.add_argument("--keys", default="<Super>r", help="The shortcut in GNOME's notation (default: <Super>r)")
    cmd_listen.add_argument("--press", action="store_true", help="Make the running listener listen now (what the shortcut does)")
    cmd_listen.add_argument("--enroll", action="store_true",
                            help="Learn your voice from you reading a short passage, so hands-free Rigved answers only you")
    cmd_listen.add_argument("--train", action="store_true",
                            help="Train voice match on the clips kept so far: you against the TV, films and other voices")
    cmd_listen.add_argument("--label", action="store_true", help="Play the clips heard hands-free and say which are you")
    cmd_listen.add_argument("--autostart", action="store_true", help="Always listen, from every login on (a systemd user service)")
    cmd_listen.add_argument("--no-autostart", action="store_true", help="Stop listening at login")
    cmd_orb = commands.add_parser("orb", help="A floating assistant that stays on screen: your stats and a chat.")
    cmd_orb.add_argument("--port", type=int, default=8765, help="Port of `rigveda serve`")
    cmd_orb.add_argument("--autostart", action="store_true", help="Keep it on screen from every login on (a systemd user service)")
    cmd_orb.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)  # how the service runs it
    cmd_orb.add_argument("--no-autostart", action="store_true", help="Stop starting it at login")
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
    if args.command == "drive":
        drive_command(args, db, config)
        return
    if args.command == "listen":
        from . import actions, desktop, voice
        if args.voice:
            if args.voice not in voice.VOICES:
                raise SystemExit(f"Choose one of: {', '.join(voice.VOICES)}")
            config.voice = args.voice
        if args.persona:
            config.persona = args.persona
        if args.mode:
            config.listen = args.mode
        if args.voice or args.persona or args.mode:
            configuration.save(config)
        if args.press:
            voice.press()
            return
        if args.enroll:
            voice.enroll()
            return
        if args.train or args.label:
            from . import voiceprint

            voiceprint.train() if args.train else voiceprint.label()
            return
        if args.no_autostart:
            print("Rigveda won't listen at login any more." if desktop.remove_listen_service() else "It wasn't set to listen at login.")
            return
        if args.autostart:
            desktop.install_listen_service()
            desktop.install_listen_key(args.keys)
            if config.listen == "wake":
                print('Rigveda is listening, and will after every login. Say "Hey Rigved", then what you want, or use '
                      "the keys. Stop with `rigveda listen --no-autostart`.")
            else:
                print(f"Press {args.keys.replace('<', '').replace('>', '+').title()} and say what you want; the microphone "
                      "is closed the rest of the time. Stop with `rigveda listen --no-autostart`.")
            return
        if args.voices:
            for name in voice.VOICES:
                print(name)
                voice.Mouth(name).say(f"Hi, I'm Rigved. This voice is called {name.split('_')[1]}.")
            return
        if args.text:
            steps = actions.understand(args.text, config)
            print("Steps:", ", ".join(f"{step.kind} {step.target!r}" for step in steps) or "none")
            reply = actions.carry_out(steps, config, dry_run=args.dry_run)
            print("Rigved:", reply)
            if not args.dry_run:
                voice.Mouth(config.voice, actions.persona(config)["speed"]).say(reply)
            return
        voice.Listener(config, voice.log).run()
        return
    if args.command == "orb":
        from . import desktop
        if args.no_autostart:
            print("The orb won't start at login any more." if desktop.remove_autostart() else "The orb wasn't set to start at login.")
            return
        if args.foreground:
            desktop.run_orb(args.port)
        if args.autostart:
            desktop.install_autostart(args.port)
        else:
            desktop.launch_orb(args.port)
        print("The orb is in the bottom-right corner of your screen: drag it anywhere, click it to open, Esc to close."
              + (" It will be there after every login and restarts itself if it crashes." if args.autostart else ""))
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
