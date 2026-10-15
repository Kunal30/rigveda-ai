"""The desktop writing window and a GNOME keyboard shortcut that opens it from any app.

GNOME on Wayland doesn't let one app read or type into another app's text boxes, so the shortcut opens a small
window instead: say what to write, get a draft from your files, copy it and paste it where you were.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import webbrowser
from urllib.error import URLError
from urllib.request import urlopen

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM = SCHEMA + ".custom-keybinding"
SLOT = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/rigveda/"
DEFAULT_KEYS = "<Super><Alt>r"
BROWSERS = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"]


def open_window(port: int) -> None:
    url = f"http://127.0.0.1:{port}/write"
    try:
        urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3).close()
    except (URLError, OSError):
        print(f"Rigveda AI isn't running on port {port}. Start it with `rigveda serve`, or enable "
              "contrib/systemd/rigveda-serve.service.", file=sys.stderr)
        raise SystemExit(1)
    browser = next((shutil.which(name) for name in BROWSERS if shutil.which(name)), None)
    if browser:  # an app window: no tabs or address bar, sized like a dialog
        subprocess.Popen([browser, f"--app={url}", "--window-size=560,620"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        webbrowser.open(url)


def _gsettings(*args: str) -> str:
    return subprocess.run(["gsettings", *args], check=True, capture_output=True, text=True).stdout.strip()


def _slots() -> list[str]:
    import ast

    value = _gsettings("get", SCHEMA, "custom-keybindings").removeprefix("@as ")
    return list(ast.literal_eval(value))


def install_hotkey(command: str, keys: str = DEFAULT_KEYS) -> None:
    """Add (or update) a GNOME custom shortcut that runs `command`."""
    if not shutil.which("gsettings"):
        raise SystemExit("gsettings not found: set a keyboard shortcut for `rigveda write` in your desktop's settings.")
    slots = _slots()
    if SLOT not in slots:
        _gsettings("set", SCHEMA, "custom-keybindings", str(slots + [SLOT]))
    for name, value in (("name", "Write with Rigveda"), ("command", command), ("binding", keys)):
        _gsettings("set", f"{CUSTOM}:{SLOT}", name, value)


def remove_hotkey() -> bool:
    if not shutil.which("gsettings"):
        return False
    slots = _slots()
    if SLOT not in slots:
        return False
    _gsettings("set", SCHEMA, "custom-keybindings", str([slot for slot in slots if slot != SLOT]))
    for name in ("name", "command", "binding"):
        _gsettings("reset", f"{CUSTOM}:{SLOT}", name)
    return True
