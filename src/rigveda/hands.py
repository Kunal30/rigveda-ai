"""Rigved's hands on the desktop: which app is in front, what can be pressed in it, pressing it, and keys.

It runs under the system Python, as the orb does, because it needs GNOME's own libraries (PyGObject), which the
virtual environment doesn't have; the voice listener starts it and talks to it a line of JSON at a time (desk.py).

- What's in front, and the buttons, menus and tabs in it, come from the accessibility tree (AT-SPI), the one screen
  readers use. Pressing one by name works on Wayland, since nothing pretends to be the mouse.
- Keys go through the desktop's Remote Desktop portal, the one way Wayland lets an app type: GNOME asks you once to
  allow it, and Rigved keeps the permission (a restore token) so it doesn't ask again.
"""

from __future__ import annotations

import json
import os
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import gi

gi.require_version("Atspi", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402

TOKEN = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "rigveda" / "remote-desktop-token"
PRESSABLE = {Atspi.Role.PUSH_BUTTON, Atspi.Role.MENU_ITEM, Atspi.Role.MENU, Atspi.Role.TOGGLE_BUTTON,
             Atspi.Role.PAGE_TAB, Atspi.Role.CHECK_BOX, Atspi.Role.RADIO_BUTTON, Atspi.Role.LINK,
             Atspi.Role.CHECK_MENU_ITEM, Atspi.Role.RADIO_MENU_ITEM, Atspi.Role.TOOL_BAR, Atspi.Role.LIST_ITEM,
             Atspi.Role.TREE_ITEM, Atspi.Role.COMBO_BOX}
MOST = 3000  # nodes looked at in one window: a web page can have tens of thousands
KEYS = {"enter": 0xff0d, "return": 0xff0d, "escape": 0xff1b, "esc": 0xff1b, "tab": 0xff09, "space": 0x20,
        "backspace": 0xff08, "delete": 0xffff, "left": 0xff51, "up": 0xff52, "right": 0xff53, "down": 0xff54,
        "home": 0xff50, "end": 0xff57, "pageup": 0xff55, "pagedown": 0xff56, "print": 0xff61,
        "comma": 0x2c, "minus": 0x2d, "plus": 0x2b, "equal": 0x3d,
        "ctrl": 0xffe3, "control": 0xffe3, "shift": 0xffe1, "alt": 0xffe9, "super": 0xffeb,
        **{f"f{n}": 0xffbe + n - 1 for n in range(1, 13)}}


# ── what's on screen ──

def _states(node) -> set:
    try:
        states = node.get_state_set()
        return {state for state in (Atspi.StateType.ACTIVE, Atspi.StateType.SHOWING, Atspi.StateType.FOCUSED)
                if states.contains(state)}
    except GLib.Error:
        return set()


def front():
    """The app and window in front: (app, window), or (None, None) if none it can see is active."""
    desktop = Atspi.get_desktop(0)
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is None:
            continue
        try:
            windows = [app.get_child_at_index(i) for i in range(app.get_child_count())]
        except GLib.Error:
            continue
        for window in windows:
            if window is not None and Atspi.StateType.ACTIVE in _states(window):
                return app, window
    return None, None


def apps() -> list[str]:
    desktop = Atspi.get_desktop(0)
    names = []
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        try:
            if app is not None and app.get_child_count() and app.get_name():
                names.append(app.get_name())
        except GLib.Error:
            continue
    return names


def controls(window) -> list:
    """What can be pressed in a window: (name, role, node), menus and their items included, closed or not."""
    found, seen, stack = [], 0, [window]
    while stack and seen < MOST:
        node = stack.pop()
        seen += 1
        try:
            role, name = node.get_role(), (node.get_name() or "").strip()
            if role in PRESSABLE and name and node.get_action_iface() is not None:
                found.append((name, role.value_nick, node))
            count = node.get_child_count()
        except GLib.Error:
            continue
        stack.extend(child for child in (node.get_child_at_index(i) for i in reversed(range(min(count, 400))))
                     if child is not None)
    return found


def _alike(asked: str, name: str) -> float:
    asked, name = asked.lower().strip(), name.lower().strip().replace("_", "")
    if asked == name:
        return 1.0
    if name.startswith(asked) or asked in name.split():
        return 0.9
    return SequenceMatcher(None, asked, name).ratio()


def press(name: str) -> dict:
    """Press the control in the window in front whose name is nearest `name`: a button, a menu, a tab."""
    app, window = front()
    if window is None:
        return {"ok": False, "why": "no window in front that I can see into"}
    asked = name.lower().removesuffix(" menu").removesuffix(" tab").removesuffix(" button").strip()
    scored = sorted(((_alike(asked, label), label, role, node) for label, role, node in controls(window)),
                    key=lambda found: -found[0])
    if not scored or scored[0][0] < 0.75:
        return {"ok": False, "why": f"nothing called {name} in {app.get_name()}",
                "near": [label for _, label, _, _ in scored[:5]]}
    _, label, role, node = scored[0]
    try:
        node.get_action_iface().do_action(0)
    except GLib.Error as error:
        return {"ok": False, "why": str(error)}
    return {"ok": True, "pressed": label, "role": role, "app": app.get_name()}


def look() -> dict:
    """The app in front, its window's title, and some of what can be pressed in it (for the model to go on)."""
    app, window = front()
    if window is None:
        return {"app": "", "window": "", "controls": [], "apps": apps()}
    labels = list(dict.fromkeys(label for label, role, _ in controls(window) if role in ("menu", "page tab", "push button")))
    return {"app": app.get_name(), "window": window.get_name() or "", "controls": labels[:60], "apps": apps()}


# ── keys, through the Remote Desktop portal ──

class Portal:
    """A Remote Desktop session with the keyboard only, asked for once and kept for as long as this runs."""

    NAME, PATH = "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"

    def __init__(self) -> None:
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.session = None
        self.count = 0

    def _request(self, method: str, signature: str, arguments: tuple, options: dict, timeout: float = 90.0) -> dict:
        """Call a portal method that answers later with a Response signal (after asking you, the first time); its
        results. `options` are GLib.Variants by name; the request's own token is added."""
        self.count += 1
        token = f"rigveda{os.getpid()}_{self.count}"
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        answer: dict = {}
        loop = GLib.MainLoop()

        def responded(connection, sender_name, object_path, interface, signal, parameters) -> None:
            code, results = parameters.unpack()
            answer.update(results, _code=code)
            loop.quit()

        subscription = self.bus.signal_subscribe(self.NAME, "org.freedesktop.portal.Request", "Response", path, None,
                                                 Gio.DBusSignalFlags.NO_MATCH_RULE, responded)
        try:
            self.bus.call_sync(self.NAME, self.PATH, "org.freedesktop.portal.RemoteDesktop", method,
                               GLib.Variant(signature, (*arguments, {**options, "handle_token": GLib.Variant("s", token)})),
                               None, Gio.DBusCallFlags.NONE, -1, None)
            GLib.timeout_add(int(timeout * 1000), loop.quit)
            loop.run()
        finally:
            self.bus.signal_unsubscribe(subscription)
        if answer.get("_code") != 0:
            raise RuntimeError("not allowed" if "_code" in answer else "no answer from the desktop")
        return answer

    def start(self) -> None:
        if self.session:
            return
        created = self._request("CreateSession", "(a{sv})", (), {"session_handle_token": GLib.Variant("s", "rigveda")})
        session = created["session_handle"]
        options = {"types": GLib.Variant("u", 1), "persist_mode": GLib.Variant("u", 2)}  # keyboard; keep the permission
        if TOKEN.exists():
            options["restore_token"] = GLib.Variant("s", TOKEN.read_text().strip())
        self._request("SelectDevices", "(oa{sv})", (session,), options)
        started = self._request("Start", "(osa{sv})", (session, ""), {})
        if started.get("restore_token"):
            TOKEN.parent.mkdir(parents=True, exist_ok=True)
            TOKEN.write_text(started["restore_token"])
            os.chmod(TOKEN, 0o600)
        self.session = session

    def key(self, keysym: int, down: bool) -> None:
        self.bus.call_sync(self.NAME, self.PATH, "org.freedesktop.portal.RemoteDesktop", "NotifyKeyboardKeysym",
                           GLib.Variant("(oa{sv}iu)", (self.session, {}, keysym, 1 if down else 0)), None,
                           Gio.DBusCallFlags.NONE, -1, None)

    def combo(self, combo: str) -> None:
        """Press keys together, "ctrl+shift+a"."""
        self.start()
        held = [KEYS.get(part, ord(part) if len(part) == 1 else None) for part in combo.lower().split("+")]
        if None in held:
            raise ValueError(f"no key called {combo}")
        for keysym in held:
            self.key(keysym, True)
        for keysym in reversed(held):
            self.key(keysym, False)
        time.sleep(0.05)

    def type(self, text: str) -> None:
        self.start()
        for char in text:
            keysym = KEYS["enter"] if char == "\n" else ord(char) if ord(char) < 0x100 else 0x01000000 + ord(char)
            self.key(keysym, True)
            self.key(keysym, False)
            time.sleep(0.01)


def main() -> None:
    """Answer requests, one JSON object per line: {"do": "look"}, {"do": "press", "name": "Export"},
    {"do": "keys", "keys": "ctrl+t"}, {"do": "type", "text": "New Project"}."""
    portal = Portal()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            what = request.get("do")
            if what == "look":
                answer = look()
            elif what == "press":
                answer = press(str(request["name"]))
            elif what == "keys":
                for combo in request["keys"] if isinstance(request["keys"], list) else [request["keys"]]:
                    portal.combo(combo)
                    time.sleep(float(request.get("gap", 0.15)))
                answer = {"ok": True}
            elif what == "type":
                portal.type(str(request["text"]))
                answer = {"ok": True}
            else:
                answer = {"ok": False, "why": f"no such request: {what}"}
        except Exception as error:  # keep answering whatever went wrong
            answer = {"ok": False, "why": f"{type(error).__name__}: {error}"}
        print(json.dumps(answer), flush=True)


if __name__ == "__main__":
    main()
