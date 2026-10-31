"""Changes that can't simply be undone, done only after you say yes to exactly what will happen: quitting programs,
putting files in the Trash, moving or renaming them, emptying the Trash, installing and removing software.

The local model names one of the forms in FORMS (machine.do); `planned` checks it against what's really there and
works out precisely what it would do ("Quit Firefox, 6 processes?"); `carry` does that, and only that, after the yes,
checking first that nothing has changed in between (the same programs, the same files). Nothing is deleted for good
by voice: files go to the Trash, and "undo that" brings them back.
"""

from __future__ import annotations

import os
import re
import signal
import threading
from pathlib import Path

FORMS = """quit <program name>   (end a program kindly, all of its processes, by its name in "Programs running" below; only if it's there)
quit <process id>...   (when one process is meant, e.g. what uses the most memory)
force quit <process id>...   (only when asked to force it, or when it won't quit)
trash <file name or path>   (put a file or folder in the Trash; it can be restored)
move <file name or path> to <folder or new name>   (move or rename it)
empty trash
install <package> | uninstall <package>   (Ubuntu software, by its package name)"""

# Programs never quit by voice: the desktop, sound, input, and Rigved itself.
KEEP = re.compile(r"^(?:gnome-|gsd-|Xwayland|mutter|systemd|pipewire|wireplumber|dbus|at-spi|xdg-|ibus|gvfs|"
                  r"evolution-|goa-|tracker|localsearch|gcr-|gpg-agent|ssh-agent|polkit|snapd)", re.I)
PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]{1,80}")
NEEDED = re.compile(r"^(?:ubuntu-(?:desktop|minimal|standard)|gnome-shell|gdm3|systemd|linux-(?:image|generic)|python3|"
                    r"network-manager|pipewire|wireplumber|sudo|apt|dpkg|snapd|nvidia-|libc6|ollama)", re.I)
MOST_REMOVED = 3  # packages an uninstall may take with it


def _signal(pid: int, number: int) -> None:
    os.kill(pid, number)


def processes() -> list[dict]:
    """Your programs, biggest first: {"pid", "name", "memory" (percent), "cpu"}, with Rigved and what it started (the
    orb's web view) left out."""
    from .machine import _run

    rows = []
    for line in _run(["ps", "-u", str(os.getuid()), "-o", "pid=,ppid=,comm=,%mem=,%cpu=,args=", "--sort=-%mem"], most=10**6).splitlines():
        parts = line.split(None, 5)
        if len(parts) >= 5 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2], float(parts[3]), float(parts[4]), parts[5] if len(parts) > 5 else ""))
    parent = {pid: ppid for pid, ppid, *_ in rows}
    ours = {pid for pid, _, _, _, _, args in rows if "rigveda" in args} | {os.getpid()}

    def rigveds(pid: int) -> bool:
        for _ in range(20):
            if pid in ours:
                return True
            if (pid := parent.get(pid, 0)) <= 1:
                return False
        return False

    return [{"pid": pid, "name": name, "memory": memory, "cpu": cpu}
            for pid, _, name, memory, cpu, _ in rows if not rigveds(pid)]


def described(seen: dict) -> str:
    shown = seen["processes"][:25] + sorted(seen["processes"], key=lambda found: -found["cpu"])[:8]
    unique = {found["pid"]: found for found in shown}.values()
    return "Programs running (yours): " + "; ".join(f"{found['pid']} {found['name']} ({found['memory']:.0f}% memory, "
                                                   f"{found['cpu']:.0f}% CPU)" for found in unique)


def _file(called: str) -> Path | str:
    """The file or folder meant, under the home folder; or why there isn't one."""
    from . import names

    called = called.strip().strip("'\"")
    path = Path(os.path.expanduser(called))
    if not path.is_absolute():
        top = Path.home() / called
        if top.exists():
            path = top
        else:
            meant = names.meant(names.matches(called))
            if len(meant) > 1:
                return "I found more than one: " + ", or ".join(names.spoken(str(found.path)) for found in meant[:3]) + ". Which one?"
            if not meant:
                return f"I couldn't find {called}."
            path = meant[0].path
    path = path.resolve()
    if not path.exists():
        return f"I couldn't find {called}."
    return path if _ours(path) else f"I'll leave {path.name or path} alone."


def _ours(path: Path) -> bool:
    """Whether a file may be moved or put in the Trash by voice: inside a folder at home, not hidden, not Rigved's."""
    home = Path.home()
    try:
        inside = path.relative_to(home).parts
    except ValueError:
        return False
    return len(inside) >= 2 and not any(part.startswith(".") for part in inside) and "rigveda" not in inside[0].lower()


def _destination(source: Path, called: str) -> Path | str:
    called = called.strip().strip("'\"")
    if "/" in called or called.startswith("~"):
        place = Path(os.path.expanduser(called))
        place = place if place.is_absolute() else Path.home() / place
    elif (folder := next((found for found in Path.home().iterdir() if found.is_dir() and found.name.lower() == called.lower()), None)):
        place = folder  # "move it to documents"
    else:  # a new name, in the same folder, keeping what kind of file it is
        place = source.with_name(called if Path(called).suffix or source.is_dir() else called + source.suffix)
    if place.is_dir():
        place = place / source.name
    place = place.resolve()
    if place.exists():
        return f"There's already a {place.name} there; I won't write over it."
    if not (_ours(place) or place.parent == Path.home()):
        return "I'll only move things within your own folders."
    return place


def planned(command: str, seen: dict) -> dict | str | None:
    """What a risky form would do, precisely, with the question to ask before doing it; why not; or None if the
    command isn't one of FORMS."""
    from .machine import _run

    text = re.sub(r"^\s*sudo\s+", "", command).strip()
    if match := re.fullmatch(r"(force )?quit ((?:\d+[ ,]*)+|[\w.+-]+)", text, re.I):
        force, asked = bool(match.group(1)), match.group(2)
        running = {found["pid"]: found for found in seen["processes"]}
        if asked.replace(",", " ").split()[0].isdigit():
            pids = [int(pid) for pid in re.findall(r"\d+", asked)]
        else:  # by name: every process of that program
            pids = [found["pid"] for found in seen["processes"] if found["name"].lower() == asked.lower()]
        chosen = [running[pid] for pid in pids if pid in running]
        if not chosen:
            return "That program isn't running."
        if any(KEEP.search(found["name"]) for found in chosen):
            return "I'll leave that running: the desktop needs it."
        named = sorted({found["name"] for found in chosen})
        how = "Force quit" if force else "Quit"
        many = f", {len(chosen)} processes" if len(chosen) > 1 else ""
        return {"kind": "quit", "force": force, "pids": {str(found["pid"]): found["name"] for found in chosen},
                "ask": f"{how} {' and '.join(named)}{many}", "said": f"{'Force quit' if force else 'Quit'} {' and '.join(named)}."}
    if match := re.fullmatch(r"(?:trash|delete|remove) (.+)", text, re.I):
        path = _file(match.group(1))
        if isinstance(path, str):
            return path
        what = f"the folder {path.name}" if path.is_dir() else path.name
        return {"kind": "trash", "path": str(path), "ask": f"Put {what} in the Trash", "said": f"{path.name} is in the Trash."}
    if match := re.fullmatch(r"(?:move|rename) (.+?) (?:to|as|into) (.+)", text, re.I):
        source = _file(match.group(1))
        if isinstance(source, str):
            return source
        place = _destination(source, match.group(2))
        if isinstance(place, str):
            return place
        renamed = place.parent == source.parent
        ask = f"Rename {source.name} to {place.name}" if renamed else f"Move {source.name} to {place.parent.name}"
        return {"kind": "move", "path": str(source), "to": str(place), "ask": ask,
                "said": f"{'Renamed' if renamed else 'Moved'}: {place.name}" + ("" if renamed else f", in {place.parent.name}") + "."}
    if re.fullmatch(r"empty (?:the )?trash", text, re.I):
        count = len([line for line in _run(["gio", "trash", "--list"]).splitlines() if line.strip() and not line.startswith("(")])
        if not count:
            return "The Trash is already empty."
        return {"kind": "empty", "ask": f"Empty the Trash, {count} thing{'s' if count != 1 else ''}, for good",
                "said": "The Trash is empty."}
    if match := re.fullmatch(r"(install|uninstall|remove package) (\S+)", text, re.I):
        verb, package = match.group(1).lower(), match.group(2).lower()
        if not PACKAGE.fullmatch(package):
            return "That isn't a package name."
        return _software(verb == "install", package)
    return None


def _software(install: bool, package: str) -> dict | str:
    """Installing or removing a package, after checking what it would really do: apt's own dry run, or snap's."""
    from .machine import _run

    snaps = {line.split()[0] for line in _run(["snap", "list"], most=10**6).splitlines()[1:] if line.split()}
    if not install:
        if NEEDED.search(package):
            return f"I'll leave {package} installed: the computer needs it."
        if package in snaps:
            return {"kind": "software", "run": ["snap", "remove", package], "ask": f"Uninstall {package}",
                    "said": f"Uninstalling {package}."}
        trial = _run(["apt-get", "-s", "remove", package], most=10**6)
        removed = re.findall(r"^Remv (\S+)", trial, re.M)
        if not removed:
            return f"{package} isn't installed."
        if len(removed) > MOST_REMOVED or any(NEEDED.search(name) for name in removed):
            return f"Removing {package} would take {len(removed)} packages with it; I'll leave that to you."
        return {"kind": "software", "run": ["apt-get", "remove", "-y", package], "ask": f"Uninstall {package}"
                + (f" and {len(removed) - 1} more" if len(removed) > 1 else ""), "said": f"Uninstalling {package}."}
    if package in snaps or re.search(rf"^ii\s+{re.escape(package)}[\s:]", _run(["dpkg", "-l", package]), re.M):
        return f"{package} is already installed."
    trial = _run(["apt-get", "-s", "install", "--no-remove", package], most=10**6)
    added = re.findall(r"^Inst (\S+)", trial, re.M)
    if added and not re.search(r"^Remv ", trial, re.M):
        return {"kind": "software", "run": ["apt-get", "install", "-y", "--no-remove", package],
                "ask": f"Install {package}" + (f" and {len(added) - 1} more packages" if len(added) > 1 else ""),
                "said": f"Installing {package}."}
    if "not found" not in (found := _run(["snap", "info", package])).lower() and found.startswith("name:"):
        return {"kind": "software", "run": ["snap", "install", package], "ask": f"Install {package} from the Snap Store",
                "said": f"Installing {package}."}
    return f"There's no package called {package}."


def carry(plan: dict, log=print, told=None) -> tuple[bool, str, dict | None]:
    """Do what was asked about, now that you said yes, if it's all still as it was: (done, said, how to undo it).
    `told(text)` is for what finishes later (software)."""
    from .machine import _act, _run

    if plan["kind"] == "quit":
        ended = 0
        for pid, name in plan["pids"].items():
            try:
                if Path(f"/proc/{pid}/comm").read_text().strip() != name or Path(f"/proc/{pid}").stat().st_uid != os.getuid():
                    continue  # it ended already, and the number may be someone else's now
                _signal(int(pid), signal.SIGKILL if plan["force"] else signal.SIGTERM)
                ended += 1
            except (OSError, ProcessLookupError):
                continue
        return bool(ended) or not plan["pids"], plan["said"] if ended else "It had already quit.", None
    if plan["kind"] == "trash":
        path = Path(plan["path"])
        if not path.exists() or not _ours(path):
            return False, f"{path.name} isn't there any more.", None
        ok, output = _act(["gio", "trash", str(path)])
        if not ok:
            return False, f"I couldn't put it in the Trash: {output[:100]}", None
        listed = _run(["gio", "trash", "--list"], most=10**6)
        uri = next((line.split("\t")[0] for line in reversed(listed.splitlines()) if line.endswith("\t" + str(path))), None)
        return True, plan["said"], {"run": ["gio", "trash", "--restore", uri], "said": plan["said"],
                               "undone": f"Okay, {path.name} is back where it was."} if uri else None
    if plan["kind"] == "move":
        source, place = Path(plan["path"]), Path(plan["to"])
        if not source.exists() or place.exists():
            return False, "Things have changed since I asked; I left it as it was.", None
        ok, output = _act(["gio", "move", str(source), str(place)])
        return (ok, plan["said"] if ok else f"I couldn't move it: {output[:100]}",
                {"run": ["gio", "move", str(place), str(source)], "said": plan["said"],
                 "undone": f"Okay, {source.name} is back where it was."} if ok else None)
    if plan["kind"] == "empty":
        ok, _ = _act(["gio", "trash", "--empty"])
        return ok, plan["said"] if ok else "I couldn't empty the Trash.", None
    if plan["kind"] == "software":
        def install() -> None:  # minutes, and GNOME asks for your password first: Rigved carries on meanwhile
            ok, output = _act(["pkexec", *plan["run"]], seconds=1800)
            log(f"{' '.join(plan['run'])}: {'done' if ok else 'failed'}: {output[-300:]}")
            done = plan["said"].replace("Installing", "Installed").replace("Uninstalling", "Uninstalled") if ok else \
                "That needs your password." if re.search(r"dismissed|authoriz|authenticat|126|127", output, re.I) else \
                f"{plan['said'].rstrip('.')} didn't work."
            _act(["notify-send", "Rigved", done], seconds=10)
            if told:
                told(done)

        threading.Thread(target=install, daemon=True).start()
        return True, plan["said"].rstrip(".") + "; GNOME will ask for your password, and I'll tell you when it's done.", None
    return False, "I don't know how to do that.", None
