"""The desktop writing window, a GNOME keyboard shortcut that opens it from any app, and the floating orb.

GNOME on Wayland doesn't let one app read or type into another app's text boxes, so the shortcut opens a small
window instead: say what to write, get a draft from your files, copy it and paste it where you were.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM = SCHEMA + ".custom-keybinding"
SLOT = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/rigveda/"
DEFAULT_KEYS = "<Super><Alt>r"
BROWSERS = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"]
SYSTEM_PYTHON = "/usr/bin/python3"  # the orb needs GTK and WebKitGTK, which only the system Python has
OLD_AUTOSTART = Path.home() / ".config" / "autostart" / "rigveda-orb.desktop"  # used before the service
ORB_UNIT = Path.home() / ".config" / "systemd" / "user" / "rigveda-orb.service"
ORB_UNIT_TEMPLATE = Path(__file__).resolve().parents[2] / "contrib" / "systemd" / "rigveda-orb.service"


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


def orb_command(port: int) -> list[str]:
    return [SYSTEM_PYTHON, str(Path(__file__).with_name("orb.py")), str(port)]


def _orb_ready() -> None:
    check = subprocess.run([SYSTEM_PYTHON, "-c", "import gi; gi.require_version('WebKit2', '4.1')"], capture_output=True)
    if check.returncode:
        raise SystemExit("The orb needs GTK and WebKitGTK for the system Python: sudo apt install python3-gi gir1.2-webkit2-4.1")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def launch_orb(port: int) -> None:
    """Start the floating orb in the background, through its service when that's installed so there's only ever one.
    It shows a dim red orb until the server answers."""
    _orb_ready()
    if ORB_UNIT.exists():
        _systemctl("start", ORB_UNIT.name)
        return
    subprocess.Popen(orb_command(port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def run_orb(port: int) -> None:
    """Run the orb in this process (for the systemd service)."""
    _orb_ready()
    os.execv(SYSTEM_PYTHON, orb_command(port))


def install_autostart(port: int) -> None:
    """Keep the orb on screen from every login on, restarting it if it crashes."""
    _orb_ready()
    if not shutil.which("systemctl"):
        raise SystemExit("systemd isn't available: add `rigveda orb` to your desktop's startup applications instead.")
    subprocess.run(["pkill", "-f", f"^{SYSTEM_PYTHON} .*rigveda/orb.py"], capture_output=True)  # a copy started by hand
    OLD_AUTOSTART.unlink(missing_ok=True)
    ORB_UNIT.parent.mkdir(parents=True, exist_ok=True)
    program = "%h/.local/bin/rigveda" if shutil.which("rigveda") else f"{sys.executable} -m rigveda"
    exec_start = f"{program} orb --foreground" + (f" --port {port}" if port != 8765 else "")
    ORB_UNIT.write_text(ORB_UNIT_TEMPLATE.read_text().replace("%h/.local/bin/rigveda orb --foreground", exec_start))
    _systemctl("daemon-reload")
    result = _systemctl("enable", "--now", ORB_UNIT.name)
    if result.returncode:
        raise SystemExit(f"Couldn't enable {ORB_UNIT.name}: {result.stderr.strip()}")


def remove_autostart() -> bool:
    removed = OLD_AUTOSTART.exists()
    OLD_AUTOSTART.unlink(missing_ok=True)
    if ORB_UNIT.exists():
        _systemctl("disable", "--now", ORB_UNIT.name)
        ORB_UNIT.unlink()
        _systemctl("daemon-reload")
        removed = True
    return removed
