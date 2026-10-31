"""Doing things on the desktop beyond opening them: what an app offers ("a new terminal window", "a private Chrome
window"), pressing a button or menu in the app in front, settings, Wi-Fi and Bluetooth, power, windows, screenshots
and typing.

Each goes the most exact way there is, as GNOME itself does it:
1. what an app declares it can do, in its launcher (.desktop) file: Terminal's "New Window", Chrome's "New Incognito
   Window"; this works whether the app is open or not;
2. its menus and buttons by name, through the accessibility tree (hands.py), in the app in front;
3. JetBrains IDEs (PyCharm): Find Action, which runs any of their menu commands by name;
4. the usual keys ("new tab" is Ctrl+T), as a last resort.
Settings go through gsettings, Wi-Fi through NetworkManager, Bluetooth through bluetoothctl and power through logind.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

SYSTEM_PYTHON = "/usr/bin/python3"  # hands.py needs GNOME's libraries, which only the system Python has
HANDS = Path(__file__).with_name("hands.py")
TOKEN = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "rigveda" / "remote-desktop-token"

# A desktop setting by what you'd call it: (schema, key, value when on, value when off).
SETTINGS = {
    "dark mode": ("org.gnome.desktop.interface", "color-scheme", "'prefer-dark'", "'default'"),
    "dark theme": ("org.gnome.desktop.interface", "color-scheme", "'prefer-dark'", "'default'"),
    "light mode": ("org.gnome.desktop.interface", "color-scheme", "'default'", "'prefer-dark'"),
    "night light": ("org.gnome.settings-daemon.plugins.color", "night-light-enabled", "true", "false"),
    "do not disturb": ("org.gnome.desktop.notifications", "show-banners", "false", "true"),
    "notifications": ("org.gnome.desktop.notifications", "show-banners", "true", "false"),
    "large text": ("org.gnome.desktop.interface", "text-scaling-factor", "1.25", "1.0"),
    "animations": ("org.gnome.desktop.interface", "enable-animations", "true", "false"),
    "hide the dock": ("org.gnome.shell.extensions.dash-to-dock", "dock-fixed", "false", "true"),
    "auto hide the dock": ("org.gnome.shell.extensions.dash-to-dock", "dock-fixed", "false", "true"),
    "screen reader": ("org.gnome.desktop.a11y.applications", "screen-reader-enabled", "true", "false"),
}
POWER = {"lock": ["loginctl", "lock-session"], "suspend": ["systemctl", "suspend"], "sleep": ["systemctl", "suspend"],
         "restart": ["systemctl", "reboot"], "reboot": ["systemctl", "reboot"], "shut down": ["systemctl", "poweroff"],
         "shutdown": ["systemctl", "poweroff"], "power off": ["systemctl", "poweroff"], "turn off": ["systemctl", "poweroff"],
         "log out": ["gnome-session-quit", "--logout", "--no-prompt"], "sign out": ["gnome-session-quit", "--logout", "--no-prompt"]}
DISRUPTIVE = {"restart", "reboot", "shut down", "shutdown", "power off", "turn off", "log out", "sign out", "close"}
WINDOW = {"maximize": "super+up", "maximise": "super+up", "restore": "super+down", "unmaximize": "super+down",
          "minimize": "super+h", "minimise": "super+h", "hide": "super+h", "left": "super+left", "left half": "super+left",
          "right": "super+right", "right half": "super+right", "full screen": "f11", "fullscreen": "f11",
          "close": "alt+f4", "next": "alt+tab", "switch": "alt+tab"}
COMMON = {"new tab": "ctrl+t", "close tab": "ctrl+w", "new window": "ctrl+n", "reopen closed tab": "ctrl+shift+t",
          "next tab": "ctrl+tab", "previous tab": "ctrl+shift+tab", "find": "ctrl+f", "search": "ctrl+f",
          "save": "ctrl+s", "reload": "f5", "refresh": "f5", "zoom in": "ctrl+=", "zoom out": "ctrl+-",
          "reset zoom": "ctrl+0", "undo": "ctrl+z", "redo": "ctrl+shift+z", "copy": "ctrl+c", "paste": "ctrl+v",
          "select all": "ctrl+a", "print": "ctrl+p", "bookmark": "ctrl+d", "history": "ctrl+h", "downloads": "ctrl+j",
          "settings": "ctrl+comma", "preferences": "ctrl+comma", "address bar": "ctrl+l", "go back": "alt+left",
          "go forward": "alt+right"}
JETBRAINS = re.compile(r"pycharm|intellij|\bidea\b|webstorm|clion|goland|rider|rubymine|phpstorm|datagrip|android studio", re.I)
ON = re.compile(r"\b(?:on|enable|enabled|start|activate|switch on|turn on|yes|true)\b")
OFF = re.compile(r"\b(?:off|disable|disabled|stop|deactivate|switch off|turn off|no|false)\b")


class Hands:
    """hands.py, started on first use and kept running (the permission to type lasts as long as it does)."""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def ask(self, request: dict, timeout: float = 15.0) -> dict:
        with self.lock:
            for attempt in range(2):
                if self.process is None or self.process.poll() is not None:
                    env = {key: value for key, value in os.environ.items() if not key.startswith("VIRTUAL_ENV")}
                    self.process = subprocess.Popen([SYSTEM_PYTHON, str(HANDS)], stdin=subprocess.PIPE,
                                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
                try:
                    self.process.stdin.write(json.dumps(request) + "\n")
                    self.process.stdin.flush()
                    answer: list[str] = []
                    reader = threading.Thread(target=lambda: answer.append(self.process.stdout.readline()), daemon=True)
                    reader.start()
                    reader.join(timeout)
                    if answer and answer[0]:
                        return json.loads(answer[0])
                    self.process.kill()  # stuck (a permission dialog nobody answered): start afresh next time
                    self.process = None
                    return {"ok": False, "why": "the desktop didn't answer in time"}
                except (OSError, ValueError):
                    self.process = None
            return {"ok": False, "why": "the desktop helper wouldn't start"}


HELPER = Hands()


def ask(request: dict, timeout: float = 15.0) -> dict:
    return HELPER.ask(request, timeout)


def look() -> dict:
    """The app in front and its window's title, what can be pressed in it, and the apps open: {"app", "window",
    "controls", "apps"}; empty when the accessibility tree can't be read."""
    answer = ask({"do": "look"}, timeout=5)
    return {"app": answer.get("app", ""), "window": answer.get("window", ""), "controls": answer.get("controls", []),
            "apps": answer.get("apps", [])}


def keys(*combos: str, gap: float = 0.15) -> bool:
    """Press keys ("ctrl+t", or several in turn) in the window in front. The first time, GNOME asks you to allow it."""
    if not TOKEN.exists():
        subprocess.run(["notify-send", "--app-name=Rigveda", "Rigved would like to press keys for you",
                        "Allow it once in the box that opens, and Rigved remembers."], capture_output=True, timeout=5)
    return bool(ask({"do": "keys", "keys": list(combos), "gap": gap}, timeout=90).get("ok"))


def type_text(text: str) -> bool:
    return bool(ask({"do": "type", "text": text}, timeout=60).get("ok"))


def press(name: str) -> dict:
    """Press the button, menu or tab called `name` in the window in front: {"ok", "pressed", "app", "why", "near"}."""
    return ask({"do": "press", "name": name})


# ── what apps offer ──

def app_actions(desktop: Path | str) -> dict[str, str]:
    """What an app's launcher says it can do, by name ("New Window" → the command for it)."""
    try:
        text = Path(desktop).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    found = {}
    for section in re.split(r"\n(?=\[)", text):
        if not section.startswith("[Desktop Action "):
            continue
        fields = dict(line.split("=", 1) for line in section.splitlines()[1:] if "=" in line and not line.startswith("#"))
        if fields.get("Name") and fields.get("Exec"):
            found[fields["Name"]] = fields["Exec"]
    return found


SAME = {"private": "incognito", "another": "new", "fresh": "new", "settings": "preferences", "options": "preferences"}
FILLER = {"a", "an", "the", "open", "me", "please", "one", "up"}


def _alike(asked: str, name: str) -> float:
    """How well what was asked for names one of an app's actions: every word said must be in its name, and its name
    may only add "new" or "window" ("private window" is New Incognito Window; "new tab" isn't New Window)."""
    def canonical(text: str) -> set[str]:
        return {SAME.get(word, word) for word in re.findall(r"[a-z]+", text.lower())} - FILLER

    said, named = canonical(asked), canonical(name)
    if said == named:
        return 1.0
    if said and said <= named and named <= said | {"new", "window"}:
        return 0.9
    return SequenceMatcher(None, asked.lower().strip(), name.lower().strip()).ratio() if said & named else 0.0


def offered(desktop: Path | str, what: str) -> tuple[str, str] | None:
    """The launcher action nearest `what` ("new private window" → ("New Incognito Window", its command))."""
    scored = sorted(((_alike(what, name), name, command) for name, command in app_actions(desktop).items()), reverse=True)
    return (scored[0][1], scored[0][2]) if scored and scored[0][0] >= 0.75 else None


def run(command: str) -> None:
    """Run a launcher's command line, without the placeholders for files it would be given (%U, %f…)."""
    from .actions import launch

    parts = [part for part in shlex.split(command) if not re.fullmatch(r"%[a-zA-Z]", part)]
    launch(*parts)


def bring_forward(app: str) -> bool:
    """Make `app` the one in front: GNOME's search, as you'd do it (Super, its name, Enter), which brings its window
    up if it's open and starts it if not."""
    if _same_app(look()["app"], app):
        return True
    ok = keys("super", gap=0.5) and type_text(app) and (time.sleep(0.7) or True) and keys("enter")
    time.sleep(0.8)
    return ok


def _same_app(seen: str, named: str) -> bool:
    seen, named = seen.lower().replace("org.gnome.", ""), named.lower()
    return bool(seen) and (seen in named or named in seen or SequenceMatcher(None, seen, named).ratio() > 0.75)


def do_in_app(app: str, what: str, desktop: Path | None, log=print) -> tuple[bool, str]:
    """Do `what` in `app` (its launcher file `desktop`, if it has one), the most exact way it can be: (done, how)."""
    if desktop is not None and (action := offered(desktop, what)):
        log(f"{app}: {what!r} → its launcher's {action[0]!r}")
        run(action[1])
        return True, action[0]
    if JETBRAINS.search(app):  # Find Action: any menu command by name
        if not bring_forward(app):
            return False, ""
        keys("ctrl+shift+a")
        time.sleep(0.5)
        type_text(what)
        time.sleep(0.9)
        keys("enter")
        log(f"{app}: {what!r} → Find Action")
        return True, what
    if not bring_forward(app):
        return False, ""
    pressed = press(what)
    if pressed.get("ok"):
        log(f"{app}: {what!r} → pressed {pressed.get('pressed')!r}")
        return True, pressed.get("pressed", what)
    combo = COMMON.get(re.sub(r"^(?:a|the|open|open a|open the) ", "", what.lower()).strip())
    if combo:
        log(f"{app}: {what!r} → keys {combo}")
        return keys(combo), what
    return False, ""


# ── settings, power, windows ──

def setting(target: str) -> tuple[bool, str]:
    """Change a setting ("dark mode on", "wifi off", "bluetooth on", or one from the catalog, "<schema> <key>
    <value>"), or put the last one back ("undo"): (done, what to say)."""
    from . import catalog

    if (picked := catalog.change(target)) is not None:
        return picked
    text = target.lower().strip()
    if re.fullmatch(r"(?:undo(?: that| it)?|put (?:it|that) back|change (?:it|that) back|revert(?: that| it)?)\W*", text):
        return catalog.undo()
    on = bool(ON.search(text)) or not OFF.search(text)
    name = re.sub(r"\b(?:on|off|enable|enabled|disable|disabled|start|stop|activate|deactivate|turn|switch|set|"
                  r"the|yes|no|true|false|please)\b", " ", text)
    name = re.sub(r"\s+", " ", name).strip()
    if re.search(r"\bwi ?-?fi\b|\bwireless\b", text):
        done = _run(["nmcli", "radio", "wifi", "on" if on else "off"])
        return done, f"Wi-Fi {'on' if on else 'off'}."
    if "bluetooth" in text:
        done = _run(["bluetoothctl", "power", "on" if on else "off"])
        return done, f"Bluetooth {'on' if on else 'off'}."
    if "bright" in text:
        return False, "The TV's brightness isn't something I can reach from here."
    scored = sorted(((SequenceMatcher(None, name, known).ratio() if known not in name else 1.0, known)
                     for known in SETTINGS), reverse=True)
    if not scored or scored[0][0] < 0.7:
        return False, ""
    schema, key, when_on, when_off = SETTINGS[scored[0][1]]
    done = catalog.put(schema, key, when_on if on else when_off)
    return done, f"{scored[0][1].capitalize()} {'on' if on else 'off'}."


def power(target: str) -> tuple[bool, str]:
    """Lock, suspend, restart, shut down or log out: (done, which)."""
    text = target.lower().strip()
    found = next((name for name in sorted(POWER, key=len, reverse=True) if name in text), "")
    return (_run(POWER[found]), found) if found else (False, "")


def window(target: str) -> tuple[bool, str]:
    """Do something to the window in front ("maximize", "left half", "close"), or "switch to <app>"."""
    text = target.lower().strip()
    if match := re.match(r"(?:switch|go|change) to (?:the )?(.+)", text):
        return bring_forward(match.group(1)), f"switched to {match.group(1)}"
    found = next((name for name in sorted(WINDOW, key=len, reverse=True) if re.search(rf"\b{name}\b", text)), "")
    return (keys(WINDOW[found]), found) if found else (False, "")


def screenshot(target: str = "") -> bool:
    """A screenshot, saved to Pictures/Screenshots and the clipboard: of the window in front, or the whole screen."""
    return keys("alt+print" if "window" in target.lower() else "shift+print")


def _run(command: list[str]) -> bool:
    try:
        return subprocess.run(command, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
