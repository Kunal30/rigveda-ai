"""The floating assistant: a small always-on-top orb that opens into a translucent panel with your stats and a chat.

The look lives in the server's /orb page; this file only gives it a frameless, see-through window that stays above
other windows. GNOME on Wayland won't let an app keep itself on top or choose where it sits, so the window runs
through XWayland, where both work. It needs GTK 3 and WebKitGTK from the system Python (python3-gi,
gir1.2-webkit2-4.1), which is why it's started as a script by `rigveda orb` rather than imported.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ["GDK_BACKEND"] = "x11"  # XWayland: keep-above and window positions work there
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")  # avoids a blank window on NVIDIA drivers

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: E402

ORB = (132, 132)  # the orb plus room for its glow
PANEL = (440, 780)
MARGIN = 24
STATE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "rigveda" / "orb.json"
OFFLINE = """<!doctype html><html><body style="margin:0;background:transparent;display:grid;place-items:center;height:100vh">
<div title="Rigveda AI isn't running" style="width:64px;height:64px;border-radius:50%;border:2px solid rgba(255,92,122,.7);
box-shadow:0 0 24px rgba(255,92,122,.45),inset 0 0 18px rgba(255,92,122,.3);background:radial-gradient(circle at 35% 30%,
rgba(255,255,255,.18),rgba(40,8,18,.75) 60%)"></div></body></html>"""


class Orb(Gtk.Window):
    def __init__(self, url: str) -> None:
        super().__init__(title="Rigveda AI")
        self.url = url
        self.expanded = False
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.stick()  # on every workspace
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        css = Gtk.CssProvider()
        css.load_from_data(b"window, webview { background: transparent; }")
        Gtk.StyleContext.add_provider_for_screen(self.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        content = WebKit2.UserContentManager()
        content.register_script_message_handler("orb")
        content.connect("script-message-received::orb", self.message)
        self.view = WebKit2.WebView.new_with_user_content_manager(content)
        self.view.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        self.view.connect("load-failed", self.offline)
        self.view.connect("context-menu", lambda *args: True)  # no "Reload / Inspect" menu on right-click
        self.add(self.view)

        self.view.set_size_request(1, 1)  # WebKit asks for 200×200 otherwise, bigger than the orb
        self.size(ORB)
        self.home = self.saved_position()
        self.move(*self.home)
        self.connect("configure-event", self.remember)
        self.connect("destroy", Gtk.main_quit)
        self.pointer = None
        GLib.timeout_add(40, self.track)
        self.view.load_uri(url)

    def track(self) -> bool:
        """Tell the page where the pointer is, relative to the orb, so the orb can lean towards it across the screen."""
        if not self.expanded and self.get_window():
            _, x, y = Gdk.Display.get_default().get_default_seat().get_pointer().get_position()
            left, top = self.get_position()
            width, height = self.get_size()
            pointer = (x - left - width // 2, y - top - height // 2)
            if pointer != self.pointer:
                self.pointer = pointer
                self.view.evaluate_javascript(f"window.look && look({pointer[0]}, {pointer[1]})", -1, None, None, None, None, None)
        return True

    def size(self, size: tuple[int, int]) -> None:
        self.set_size_request(*size)
        self.resize(*size)

    def workarea(self) -> Gdk.Rectangle:
        display = Gdk.Display.get_default()
        monitor = display.get_monitor_at_window(self.get_window()) if self.get_window() else display.get_primary_monitor()
        return (monitor or display.get_monitor(0)).get_workarea()

    def saved_position(self) -> tuple[int, int]:
        try:
            x, y = json.loads(STATE.read_text())["position"]
            return int(x), int(y)
        except (OSError, ValueError, KeyError, TypeError):
            area = self.workarea()  # bottom-right corner by default
            return area.x + area.width - ORB[0] - MARGIN, area.y + area.height - ORB[1] - MARGIN

    def remember(self, widget, event) -> bool:
        if not self.expanded:
            self.home = (event.x, event.y)
            try:
                STATE.parent.mkdir(parents=True, exist_ok=True)
                STATE.write_text(json.dumps({"position": self.home}))
            except OSError:
                pass
        return False

    def message(self, manager, result) -> None:
        try:
            request = json.loads(result.get_js_value().to_string())
        except (ValueError, AttributeError):
            return
        command = request.get("cmd")
        if command == "drag" and not self.expanded:
            seat = Gdk.Display.get_default().get_default_seat()
            _, x, y = seat.get_pointer().get_position()
            self.begin_move_drag(1, x, y, Gtk.get_current_event_time())
        elif command == "expand":
            self.expand()
        elif command == "collapse":
            self.collapse()
        elif command == "open":
            Gtk.show_uri_on_window(self, request.get("url", self.url), Gtk.get_current_event_time())
        elif command == "quit":
            self.destroy()

    def expand(self) -> None:
        """Grow into the panel towards the middle of the screen, so it stays fully visible wherever the orb sits."""
        if self.expanded:
            return
        self.expanded = True
        area = self.workarea()
        x, y = self.home
        if x + ORB[0] / 2 > area.x + area.width / 2:
            x = x + ORB[0] - PANEL[0]
        if y + ORB[1] / 2 > area.y + area.height / 2:
            y = y + ORB[1] - PANEL[1]
        x = min(max(x, area.x + 8), area.x + area.width - PANEL[0] - 8)
        y = min(max(y, area.y + 8), area.y + area.height - PANEL[1] - 8)
        self.size(PANEL)
        self.move(x, y)
        self.present()

    def collapse(self) -> None:
        if not self.expanded:
            return
        self.size(ORB)
        self.move(*self.home)
        GLib.timeout_add(200, self.settled)

    def settled(self) -> bool:
        self.expanded = False  # only now, so the shrink itself isn't saved as the orb's position
        return False

    def offline(self, view, event, uri, error) -> bool:
        """Show a dim red orb while the server is down, and try again every 10 seconds."""
        self.view.load_html(OFFLINE, None)
        GLib.timeout_add_seconds(10, lambda: self.view.load_uri(self.url) and False)
        return True


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    Orb(f"http://127.0.0.1:{port}/orb?native=1").show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
